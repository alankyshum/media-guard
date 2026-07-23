#!/usr/bin/env python3
"""
extract.py — deterministic local media -> text dispatcher for opencode's
media-guard plugin. Consolidates the extraction logic that used to live in the
`tool--pdf` and `tool--transcribe` skills so attachments can be parsed inline
without shipping raw bytes to a remote model.

Usage:
    extract.py <file> [--mime MIME] [--kind KIND] [--max-chars N]
                [--timeout SECONDS] [--model base] [--no-cache]
                [--ocr-mode baseline|correction]

Always prints a single JSON object to stdout and exits 0 (errors are reported
in the JSON, never via exit code, so the caller never has to parse stderr):

    {
      "status": "ok" | "unavailable" | "too_large" | "timeout" | "error" | "not_found",
      "kind":   "pdf" | "audio" | "video" | "image" | "text" | "unknown",
      "text":   "<extracted text>",          # present when status == ok
      "truncated": false,
      "chars": 1234,
      "tool":  "pymupdf" | "pdftotext" | "pypdf" | "whisper" | "tesseract" | "plain",
      "cached": false,
      "meta":  { ... },
      "detail": "human readable reason (on non-ok)"
    }

Backends (all optional; missing ones degrade to status=unavailable):
    pdf    -> pdftotext (poppler) | PyMuPDF (fitz) | pypdf
    audio  -> whisper CLI (ffmpeg-decoded)
    video  -> whisper CLI (ffmpeg extracts audio)
    image  -> tesseract OCR (macOS sips converts HEIC when Pillow cannot read it)
    text   -> read directly

Expensive results (transcription/OCR) are cached on disk keyed by
sha256(path,size,mtime,kind,model,max_chars) so restarts and repeated turns are
cheap.
"""
import argparse
import base64
import hashlib
import json
import math
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile

CACHE_DIR = os.environ.get("MEDIA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "opencode-media-cache"))
CACHE_VERSION = "v5-sticky-resident-indexed"
IMAGE_PREPROCESS_VERSION = "v2-deterministic-rotation"
STICKY_FRAGMENT_VERSION = "v1-fragment-seed-physical-refinement"
STICKY_RESIDENT_VERSION = "v1-discovery-indexed-verification"
STICKY_RESIDENT_KEEP_ALIVE = "10m"
STICKY_MULTICROP_VERSION = "v2-local-candidate-presence"
MULTICROP_FAILURE_CODES = {
    "request-exception": "MULTICROP_REQUEST_EXCEPTION",
    "crop": "MULTICROP_CROP_FAILURE",
    "deadline": "MULTICROP_DEADLINE",
    "schema": "MULTICROP_SCHEMA_INVALID",
    "error": "MULTICROP_ERROR",
}
CACHE_TTL_SECONDS = int(os.environ.get("MEDIA_CACHE_TTL_SECONDS", "86400"))
CACHE_MAX_ENTRIES = int(os.environ.get("MEDIA_CACHE_MAX_ENTRIES", "256"))
CACHE_LOCK = threading.Lock()

PDF_MIMES = {"application/pdf"}
AUDIO_PREFIX = "audio/"
VIDEO_PREFIX = "video/"
IMAGE_PREFIX = "image/"

PDF_EXT = {".pdf"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".flv", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif"}
HEIC_EXT = {".heic", ".heif"}
IMAGE_MAX_PIXELS = int(os.environ.get("MEDIA_IMAGE_MAX_PIXELS", str(40_000_000)))
NOTE_MAX_REGIONS = int(os.environ.get("MEDIA_NOTE_MAX_REGIONS", "20"))
NOTE_MAX_CROP_PIXELS = int(os.environ.get("MEDIA_NOTE_MAX_CROP_PIXELS", "4_000_000"))
NOTE_MIN_COLORFULNESS = float(os.environ.get("MEDIA_NOTE_MIN_COLORFULNESS", "0.10"))
NOTE_MIN_CANDIDATE_SCORE = float(os.environ.get("MEDIA_NOTE_MIN_CANDIDATE_SCORE", "0.15"))
NOTE_TILED_PROPOSALS = os.environ.get("MEDIA_NOTE_TILED_PROPOSALS", "0").strip().lower() in {"1", "true", "yes"}
NOTE_TILE_MAX_PROPOSALS = 32
NOTE_TILE_MAX_TILES = 64
NOTE_TILE_SIZE = int(os.environ.get("MEDIA_NOTE_TILE_SIZE", "768"))
NOTE_TILE_OVERLAP = float(os.environ.get("MEDIA_NOTE_TILE_OVERLAP", "0.20"))
STICKY_MULTICROP_MAX_REQUESTS = int(os.environ.get("OCR_STICKY_MULTICROP_MAX_REQUESTS", "8"))
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml", ".srt", ".vtt"}

ARCHIVE_EXT = {".zip"}
ZIP_MIMES = {
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "application/zip-compressed",
    "multipart/x-zip",
}

# Zip safety caps (guard against zip bombs / resource exhaustion)
ZIP_MAX_FILES = 512
ZIP_MAX_TOTAL_BYTES = 512 * 1024 * 1024  # 512 MiB uncompressed

OCR_VLM_BASE_URL = os.environ.get("OCR_VLM_BASE_URL", "http://127.0.0.1:11434")
OCR_VLM_MODEL = os.environ.get("OCR_VLM_MODEL", "qwen2.5vl:32b")
OCR_CORRECTION_MODEL = os.environ.get("OCR_CORRECTION_MODEL", "qwen2.5vl:7b")
GRANITE_DOCLING_BASE_URL = os.environ.get("GRANITE_DOCLING_BASE_URL", "http://127.0.0.1:11434")
GRANITE_DOCLING_MODEL = os.environ.get("GRANITE_DOCLING_MODEL", "granite-docling:latest")


def _apple_vision_cache_path():
    source = os.path.join(os.path.dirname(__file__), "apple-vision-ocr.swift")
    try:
        with open(source, "rb") as f:
            source_hash = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        source_hash = "unknown"
    cache_root = os.environ.get(
        "MEDIA_GUARD_CACHE_DIR",
        os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/Library/Caches")), "opencode", "media-guard"),
    )
    return os.path.join(cache_root, f"apple-vision-ocr-{source_hash}")


def _extractor_audit(path, mime, timeout=1.0):
    source_path = os.path.abspath(__file__)
    try:
        with open(path, "rb") as source:
            input_sha256 = hashlib.sha256(source.read()).hexdigest()
        input_bytes = os.path.getsize(path)
    except OSError:
        input_sha256 = "unavailable"
        input_bytes = None
    try:
        with open(source_path, "rb") as source:
            extractor_sha256 = hashlib.sha256(source.read()).hexdigest()
    except OSError:
        extractor_sha256 = "unavailable"
    dimensions = None
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as image:
            dimensions = [image.width, image.height]
    except (OSError, ImportError):
        pass
    if (not dimensions or any(value is None for value in dimensions)) and os.path.splitext(path)[1].lower() in HEIC_EXT and sys.platform == "darwin" and which("sips"):
        try:
            width, height = _sips_dimensions(path, timeout)
            if width > 0 and height > 0:
                dimensions = [width, height]
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    return {
        "input_sha256": input_sha256,
        "input_mime": mime,
        "input_bytes": input_bytes,
        "input_dimensions": dimensions,
        "extractor_source_path": source_path,
        "extractor_source_sha256": extractor_sha256,
    }


APPLE_VISION_OCR_BIN = os.environ.get("APPLE_VISION_OCR_BIN", _apple_vision_cache_path())
OCR_VLM_NUM_CTX = int(os.environ.get("OCR_VLM_NUM_CTX", "16384"))
try:
    OCR_PDF_MAX_PAGES = int(os.environ.get("OCR_PDF_MAX_PAGES", "20"))
except (TypeError, ValueError):
    OCR_PDF_MAX_PAGES = 20


def _vlm_enabled():
    return os.environ.get("OCR_VLM_ENABLED", "0").strip().lower() not in {"0", "false"}


def _local_url(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _granite_docling_enabled():
    return os.environ.get("GRANITE_DOCLING_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


CLASSIFICATION_LABELS = {"document", "handwriting", "photo", "sticky_notes"}


def _ollama_json(url, payload, timeout):
    if not _local_url(url):
        raise urllib.error.URLError("remote inference disabled")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
        return json.loads(response.read().decode("utf-8"))


def _granite_docling_available(timeout=1.0):
    """Return true only when configured Granite-Docling tag is installed locally."""
    if not _granite_docling_enabled():
        return False, {"enabled": False, "reason": "disabled"}
    if not _local_url(GRANITE_DOCLING_BASE_URL):
        return False, {"enabled": True, "model": GRANITE_DOCLING_MODEL, "reason": "remote-inference-disabled"}
    try:
        request = urllib.request.Request(GRANITE_DOCLING_BASE_URL.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tags = {item.get("name") for item in payload.get("models", []) if isinstance(item, dict)}
        available = GRANITE_DOCLING_MODEL in tags
        return available, {"enabled": True, "model": GRANITE_DOCLING_MODEL, "installed_tags": sorted(t for t in tags if t), "reason": "installed" if available else "model-not-installed"}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as error:
        return False, {"enabled": True, "model": GRANITE_DOCLING_MODEL, "reason": f"ollama-unavailable: {error}"}


def _parse_granite_classification(payload):
    content = payload.get("message", {}).get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("label") not in CLASSIFICATION_LABELS:
        return None
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not 0 <= confidence <= 1:
        return None
    features = parsed.get("features", {})
    if not isinstance(features, dict):
        return None
    return {"label": parsed["label"], "confidence": confidence, "features": features}


def _granite_docling_classify(path, timeout=5.0):
    available, availability = _granite_docling_available(min(timeout, 1.0))
    if not available:
        return None, {"engine": "deterministic", "granite_docling": availability}
    try:
        with open(path, "rb") as source:
            image = base64.b64encode(source.read()).decode("ascii")
        prompt = (
            "Classify this image. Return ONLY JSON: "
            '{"label":"document|handwriting|photo|sticky_notes","confidence":0.0,"features":{}}. '
            "document means typed or printed page; handwriting means handwritten page or note; "
            "sticky_notes means one or more colored sticky notes; photo means ordinary photograph or other image. "
            "Features must contain only observable booleans/numbers/short strings. Never guess text."
        )
        payload = _ollama_json(
            GRANITE_DOCLING_BASE_URL.rstrip("/") + "/api/chat",
            {"model": GRANITE_DOCLING_MODEL, "messages": [{"role": "user", "content": prompt, "images": [image]}], "stream": False, "think": False, "format": "json", "options": {"temperature": 0}},
            max(0.1, timeout),
        )
        parsed = _parse_granite_classification(payload)
        if parsed is None:
            return None, {"engine": "deterministic", "granite_docling": {**availability, "reason": "invalid-response"}}
        return parsed, {"engine": "granite-docling", "model": GRANITE_DOCLING_MODEL, "granite_docling": availability}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as error:
        return None, {"engine": "deterministic", "granite_docling": {**availability, "reason": f"inference-failed: {error}"}}


def _classify_image(path, timeout=5.0):
    """Granite-Docling-first image routing with deterministic fail-closed fallback."""
    granite, granite_meta = _granite_docling_classify(path, timeout)
    if granite:
        return granite, granite_meta

    document, document_features = _document_image(path, timeout)
    if document:
        return {"label": "document", "confidence": 0.8, "features": document_features}, granite_meta
    note_workdir = tempfile.mkdtemp(prefix="oc-classify-notes-")
    try:
        regions, note_features = _note_regions(path, note_workdir)
        if regions:
            return {"label": "sticky_notes", "confidence": min(0.95, 0.55 + len(regions) * 0.05), "features": note_features}, granite_meta
    except Exception:
        pass
    finally:
        shutil.rmtree(note_workdir, ignore_errors=True)
    return {"label": "photo", "confidence": 0.5, "features": document_features}, granite_meta


def _correction_enabled():
    return os.environ.get("OCR_CORRECTION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def _sticky_resident_enabled():
    return os.environ.get("OCR_STICKY_RESIDENT_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def _sticky_multicrop_enabled():
    return os.environ.get("OCR_STICKY_MULTICROP_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def _sticky_route():
    """Return one sticky route; multicrop is exclusive when enabled."""
    if _sticky_multicrop_enabled():
        return "multicrop"
    if _sticky_resident_enabled():
        return "resident-indexed"
    return "legacy"


def which(name):
    return shutil.which(name)


def classify(path, mime, kind_hint):
    if kind_hint and kind_hint != "auto":
        return kind_hint
    m = (mime or "").lower()
    if m in PDF_MIMES:
        return "pdf"
    if m.startswith(AUDIO_PREFIX):
        return "audio"
    if m.startswith(VIDEO_PREFIX):
        return "video"
    if m.startswith(IMAGE_PREFIX):
        return "image"
    if m in ZIP_MIMES:
        return "archive"
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in TEXT_EXT or (m.startswith("text/")):
        return "text"
    return "unknown"


def cache_key(path, kind, model, max_chars, correction_enabled=None, ocr_engine="production"):
    if correction_enabled is None:
        correction_enabled = _correction_enabled()
    resident_enabled = _sticky_resident_enabled()
    ocr_sig = (
        f"{IMAGE_PREPROCESS_VERSION}:{_vlm_enabled()}:{OCR_VLM_MODEL}:{OCR_PDF_MAX_PAGES}:"
        f"{correction_enabled}:{OCR_CORRECTION_MODEL}:resident={resident_enabled}:"
        f"resident_route={STICKY_RESIDENT_VERSION}:resident_model={OCR_VLM_MODEL}:"
        f"resident_base_url={OCR_VLM_BASE_URL}:resident_keep_alive={STICKY_RESIDENT_KEEP_ALIVE}:"
        f"multicrop={_sticky_multicrop_enabled()}:{STICKY_MULTICROP_VERSION}:{STICKY_MULTICROP_MAX_REQUESTS}"
    )
    try:
        with open(path, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()
        sig = f"{CACHE_VERSION}|{content_hash}|{kind}|{model}|{max_chars}|{ocr_engine}|{ocr_sig}"
    except OSError:
        sig = f"{os.path.abspath(path)}|nostat|{kind}|{model}|{max_chars}|{ocr_engine}|{ocr_sig}"
    return hashlib.sha256(sig.encode()).hexdigest()


def cache_get(key):
    p = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("cache_version") != CACHE_VERSION:
            return None
        if CACHE_TTL_SECONDS >= 0 and time.time() - float(obj.get("cached_at", 0)) > CACHE_TTL_SECONDS:
            os.remove(p)
            return None
        return obj.get("result")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def cache_put(key, obj):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, key + ".json")
        with CACHE_LOCK:
            tmp = tempfile.NamedTemporaryFile(prefix=key + ".", suffix=".tmp", dir=CACHE_DIR, mode="w", encoding="utf-8", delete=False)
            try:
                with tmp:
                    json.dump({"cache_version": CACHE_VERSION, "cached_at": time.time(), "result": obj}, tmp)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp.name, p)
            finally:
                try: os.unlink(tmp.name)
                except OSError: pass
            entries = sorted(
                (os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR) if n.endswith(".json")),
                key=lambda item: os.stat(item).st_mtime,
            )
            for stale in entries[:-max(1, CACHE_MAX_ENTRIES)]:
                try: os.remove(stale)
                except OSError: pass
    except Exception:
        pass


def _apply_validated_corrections(text, payload, min_confidence=0.85):
    """Apply only exact, non-overlapping OCR span corrections with provenance."""
    try:
        corrections = payload.get("corrections", [])
        if not isinstance(corrections, list):
            return text, {"status": "abstain", "reason": "invalid corrections list"}
        accepted = []
        rejected = 0
        for item in corrections:
            if not isinstance(item, dict):
                rejected += 1
                continue
            source = item.get("source")
            replacement = item.get("replacement")
            confidence = float(item.get("confidence", 0))
            provenance = item.get("provenance")
            if not isinstance(source, str) or not isinstance(replacement, str) or not source:
                rejected += 1
                continue
            if confidence < min_confidence or confidence > 1 or not isinstance(provenance, dict) or not provenance:
                rejected += 1
                continue
            start = item.get("start")
            if isinstance(start, bool) or not isinstance(start, int):
                start = text.find(source)
            if start < 0 or text[start:start + len(source)] != source:
                rejected += 1
                continue
            end = start + len(source)
            if any(start < old_end and end > old_start for old_start, old_end, *_ in accepted):
                rejected += 1
                continue
            accepted.append((start, end, replacement, item))
        accepted.sort(key=lambda item: item[0])
        corrected = text
        for start, end, replacement, _ in reversed(accepted):
            corrected = corrected[:start] + replacement + corrected[end:]
        meta = {"status": "corrected" if accepted else "abstain", "accepted_spans": [item[3] for item in accepted]}
        if rejected:
            meta["rejected_spans"] = rejected
        if not accepted and rejected:
            meta["reason"] = "all proposed spans failed exact source/confidence/provenance validation"
        return corrected, meta
    except (AttributeError, TypeError, ValueError):
        return text, {"status": "abstain", "reason": "invalid correction payload"}


def correct_ocr_text(text, timeout):
    if not _correction_enabled() or not text.strip():
        return text, {"status": "disabled"}
    schema = {
        "type": "object",
        "required": ["corrections"],
        "additionalProperties": False,
        "properties": {
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source", "replacement", "confidence", "provenance"],
                    "additionalProperties": False,
                    "properties": {
                        "source": {"type": "string"},
                        "replacement": {"type": "string"},
                        "start": {"type": "integer", "minimum": 0},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "provenance": {"type": "object"},
                    },
                },
            },
        },
    }
    prompt = (
        "Correct only clear OCR errors in supplied text. Source must be copied verbatim "
        "from text. If providing start, it must be the zero-based Python character index; omit start if unsure. "
        "confidence must be a decimal from 0 to 1, never a percentage. Return JSON matching schema exactly. Use corrections=[] when uncertain. "
        "Do not summarize or explain.\n\n" + json.dumps({"text": text}, ensure_ascii=False)
    )
    body = {
        "model": OCR_CORRECTION_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": OCR_VLM_NUM_CTX},
    }
    request = urllib.request.Request(OCR_VLM_BASE_URL.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw = json.loads(((payload.get("message") or {}).get("content") or ""))
        return _apply_validated_corrections(text, raw)
    except (socket.timeout, TimeoutError):
        return text, {"status": "timeout"}
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return text, {"status": "timeout"}
        return text, {"status": "abstain", "reason": str(e.reason)[-200:]}
    except Exception as e:
        return text, {"status": "abstain", "reason": str(e)[-200:]}


def correction_fields(original_text, corrected_text):
    return {
        "text": original_text,
        "original_text": original_text,
        "corrected_text": corrected_text if corrected_text != original_text else None,
    }


def bounded_correction_fields(original_text, corrected_text, max_chars):
    """Keep authoritative and alternative inline text within one aggregate cap."""
    limit = max(0, int(max_chars))
    bounded_original = original_text[:limit]
    remaining = max(0, limit - len(bounded_original))
    bounded_corrected = corrected_text[:remaining]
    return correction_fields(bounded_original, bounded_corrected), {
        "original_truncated": len(bounded_original) < len(original_text),
        "corrected_truncated": len(bounded_corrected) < len(corrected_text),
    }


def run(cmd, timeout):
    """Run a command, return (rc, stdout, stderr) or raise TimeoutExpired."""
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _fitz_python():
    """Return an interpreter that can import fitz, or None."""
    candidates = [
        os.path.expanduser("~/Documents/dotfiles/config/claude-code/skills/tool--pdf/scripts/.venv/bin/python"),
        os.path.expanduser("~/.claude/skills/tool--pdf/scripts/.venv/bin/python"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    if sys.executable:
        try:
            rc, _, _ = run([sys.executable, "-c", "import fitz"], 2)
            if rc == 0:
                return sys.executable
        except Exception:
            pass
    return None


# ---------- PDF ----------
def extract_pdf(path, timeout):
    # 1) pdftotext (poppler)
    if which("pdftotext"):
        try:
            rc, out, _ = run(["pdftotext", "-layout", path, "-"], timeout)
            if rc == 0 and out.strip():
                return out, "pdftotext", {}
        except subprocess.TimeoutExpired:
            raise
        except Exception:
            pass
    # 2) PyMuPDF
    try:
        import fitz  # type: ignore
        with fitz.open(path) as doc:
            txt = "\n\n".join(pg.get_text() for pg in doc)
            if txt.strip():
                return txt, "pymupdf", {"pages": doc.page_count}
    except Exception:
        pass
    # 3) pypdf
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(path)
        txt = "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
        if txt.strip():
            return txt, "pypdf", {"pages": len(reader.pages)}
    except Exception:
        pass
    # 4) OCR rendered PDF pages with the local vision model. fitz is deliberately
    # loaded in its skill venv, not in this runtime process.
    fpy = _fitz_python()
    if not fpy:
        return None, None, {"why": "no fitz-capable interpreter for scanned-pdf OCR"}

    raster_prog = r'''import json, os, sys, fitz
pdf_path, out_dir, max_pages = sys.argv[1:]
doc = fitz.open(pdf_path)
paths = []
for page_num in range(min(doc.page_count, int(max_pages))):
    pix = doc[page_num].get_pixmap(matrix=fitz.Matrix(2, 2))
    png_path = os.path.join(out_dir, "page_%03d.png" % (page_num + 1))
    pix.save(png_path)
    paths.append(png_path)
print(json.dumps(paths))'''
    page_count = 0
    vlm_meta = {}
    tmpdir = tempfile.mkdtemp(prefix="oc-pdf-ocr-")
    try:
        deadline = time.monotonic() + timeout
        try:
            raster_timeout = max(1.0, deadline - time.monotonic())
            rc, out, err = run([fpy, "-c", raster_prog, path, tmpdir, str(OCR_PDF_MAX_PAGES)], raster_timeout)
            if rc != 0:
                return None, None, {"why": f"PDF rasterization failed: {(err or out or 'unknown error')[-400:]}"}
            png_paths = json.loads(out)
            page_count = len(png_paths)
        except subprocess.TimeoutExpired:
            raise
        except Exception as e:
            return None, None, {"why": f"PDF rasterization unavailable: {e}"}

        if _vlm_enabled():
            page_text = []
            failed_pages = []
            for page_num, png_path in enumerate(png_paths, 1):
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    skipped_pages = list(range(page_num, page_count + 1))
                    failed_pages.extend(skipped_pages)
                    vlm_meta["skipped_pages"] = skipped_pages
                    break
                try:
                    page_text_item, _, page_meta = _run_vlm_ocr(png_path, min(remaining, timeout))
                    if page_text_item and page_text_item.strip():
                        page_text.append((page_num, page_text_item.strip()))
                    else:
                        failed_pages.append(page_num)
                        vlm_meta = page_meta or vlm_meta
                        if page_meta and page_meta.get("timeout"):
                            skipped_pages = list(range(page_num + 1, page_count + 1))
                            failed_pages.extend(skipped_pages)
                            vlm_meta["skipped_pages"] = skipped_pages
                            break
                except Exception:
                    failed_pages.append(page_num)
            if page_text:
                joined = "\n\n".join(
                    f"----- Page {page_num} -----\n\n{text}"
                    for page_num, text in page_text
                )
                return joined, "qwen3-vl-ocr-pdf", {
                    "pages": page_count,
                    "ocr_pages": len(page_text),
                    "failed_pages": failed_pages,
                    **({"vlm": vlm_meta} if vlm_meta else {}),
                    "rasterizer": fpy,
                }

        # 5) Cheap local OCR fallback, also executed in fitz's interpreter.
        local_prog = r'''import json, sys, fitz
doc = fitz.open(sys.argv[1])
items, failed = [], []
for page_num in range(min(doc.page_count, int(sys.argv[2]))):
    try:
        page = doc[page_num]
        textpage = page.get_textpage_ocr(flags=0, language="chi_sim+chi_tra")
        text = page.get_text("text", textpage=textpage)
        if text and text.strip(): items.append([page_num + 1, text.strip()])
        else: failed.append(page_num + 1)
    except Exception: failed.append(page_num + 1)
print(json.dumps({"items": items, "failed": failed}))'''
        try:
            remaining = max(1.0, deadline - time.monotonic())
            rc, out, err = run([fpy, "-c", local_prog, path, str(page_count)], remaining)
            if rc != 0:
                local_result = {"items": [], "failed": list(range(1, page_count + 1))}
            else:
                local_result = json.loads(out)
        except subprocess.TimeoutExpired:
            raise
        except Exception:
            local_result = {"items": [], "failed": list(range(1, page_count + 1))}
        local_text = [(int(n), text) for n, text in local_result.get("items", [])]
        local_failed = local_result.get("failed", [])
        if local_text:
            joined = "\n\n".join(
                f"----- Page {page_num} -----\n\n{text}" for page_num, text in local_text
            )
            return joined, "pymupdf-ocr", {
                "pages": page_count,
                "ocr_pages": len(local_text),
                "failed_pages": local_failed,
                "rasterizer": fpy,
            }
        why = vlm_meta.get("why", "vision OCR produced no text") if vlm_meta else "PyMuPDF OCR produced no text"
        return None, None, {"why": why, "pages": page_count, "failed_pages": local_failed, "vlm": vlm_meta, "rasterizer": fpy}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- audio / video via whisper ----------
def extract_whisper(path, model, timeout):
    if not which("whisper"):
        return None, None, {"why": "whisper CLI not installed"}
    outdir = tempfile.mkdtemp(prefix="oc-whisper-")
    cmd = [
        "whisper", path,
        "--model", model,
        "--output_format", "txt",
        "--output_dir", outdir,
        "--verbose", "False",
    ]
    try:
        rc, out, err = run(cmd, timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(outdir, ignore_errors=True)
        raise
    text = None
    try:
        for fn in os.listdir(outdir):
            if fn.endswith(".txt"):
                with open(os.path.join(outdir, fn)) as f:
                    text = f.read()
                break
    finally:
        shutil.rmtree(outdir, ignore_errors=True)
    if rc == 0 and text and text.strip():
        return text, "whisper", {"model": model}
    return None, None, {"why": (err or out or "whisper produced no text")[-400:]}


def transcribe_via_skill(audio_path, timeout, model="turbo"):
    SKILL_DIR = os.path.expanduser("~/.claude/skills/tool--transcribe/scripts")
    script_path = os.path.join(SKILL_DIR, "transcribe_audio.py")
    if not os.path.exists(script_path):
        return None, None, {"why": f"transcribe_audio.py not found at {script_path}"}

    venv_py = os.path.join(SKILL_DIR, ".venv/bin/python")
    if os.path.exists(venv_py):
        py = venv_py
    elif sys.executable:
        py = sys.executable
    else:
        py = "python3"

    tmpdir = tempfile.mkdtemp(prefix="oc-transcribe-")
    cmd = [py, script_path, audio_path, "--formats", "txt", "--model", model, "--backend", "auto", "--output-dir", tmpdir]
    try:
        try:
            rc, out, err = run(cmd, timeout)
        except subprocess.TimeoutExpired:
            raise
        text = None
        if rc == 0:
            for fn in os.listdir(tmpdir):
                if fn.endswith(".txt"):
                    with open(os.path.join(tmpdir, fn), "r", errors="replace") as f:
                        text = f.read()
                    break
        if rc == 0 and text is not None and text.strip():
            return text, "transcribe-skill", {"model": model, "backend": "auto"}
        why = f"rc={rc}"
        if err and err.strip():
            why += f", err={err[-200:].strip()}"
        elif out and out.strip():
            why += f", out={out[-200:].strip()}"
        else:
            why += ", no txt produced"
        return None, None, {"why": f"transcribe_via_skill failed: {why}"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def extract_video(path, model, timeout):
    if which("crv"):
        outdir = tempfile.mkdtemp(prefix="oc-crv-")
        cmd = ["crv", path, "-o", outdir, "--no-transcribe", "--keep-audio", "--max-frames", "24"]
        try:
            rc, out, err = run(cmd, timeout)
        except subprocess.TimeoutExpired:
            raise

        frames = []
        audio = None
        manifest = None
        for root, dirs, files in os.walk(outdir):
            for f in files:
                f_low = f.lower()
                full_p = os.path.join(root, f)
                if f_low.endswith((".jpg", ".jpeg", ".png")):
                    frames.append(full_p)
                elif f_low == "audio.m4a" or f_low.endswith((".m4a", ".wav", ".aac")):
                    if audio is None or f_low == "audio.m4a":
                        audio = full_p
                elif "manifest" in f_low:
                    manifest = full_p
        frames.sort()

        transcript_text = ""
        if audio:
            try:
                skill_model = "turbo" if model == "base" else model
                text, _, _ = transcribe_via_skill(audio, timeout, skill_model)
                if text:
                    transcript_text = text
            except subprocess.TimeoutExpired:
                raise
            except Exception:
                pass

        return (
            transcript_text,
            "crv+transcribe-skill",
            {
                "frames": frames,
                "manifest": manifest,
                "audio": audio,
                "frame_count": len(frames)
            }
        )

    elif which("ffmpeg"):
        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="oc-ffmpeg-")
        os.close(fd)
        cmd = ["ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav]
        try:
            try:
                rc, out, err = run(cmd, timeout)
            except subprocess.TimeoutExpired:
                raise
            if rc == 0:
                skill_model = "turbo" if model == "base" else model
                text, _, _ = transcribe_via_skill(wav, timeout, skill_model)
                if text:
                    return (text, "ffmpeg+transcribe-skill", {"frames": [], "audio": None, "degraded": "crv-unavailable"})
                else:
                    return (None, None, {"why": "ffmpeg audio extracted but transcription failed", "frames": [], "audio": None, "degraded": "crv-unavailable"})
            else:
                return (None, None, {"why": f"ffmpeg failed with rc={rc}, err={err[-200:].strip() if err else ''}", "frames": [], "degraded": "crv-unavailable"})
        except subprocess.TimeoutExpired:
            raise
        except Exception as e:
            return (None, None, {"why": f"ffmpeg degradation error: {str(e)}", "frames": [], "degraded": "crv-unavailable"})
        finally:
            try:
                os.remove(wav)
            except Exception:
                pass

    else:
        return None, None, {"why": "crv & ffmpeg unavailable"}


# ---------- image via tesseract ----------
def _run_vlm_ocr(img_path, timeout):
    if not _vlm_enabled():
        return None, None, {"why": "vlm ocr disabled"}
    if not _local_url(OCR_VLM_BASE_URL):
        return None, None, {"why": "remote inference disabled"}
    try:
        with open(img_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
        body = {
            "model": OCR_VLM_MODEL,
            "messages": [{
                "role": "user",
                "content": (
                    "Transcribe ALL visible text faithfully. Preserve reading order "
                    "and line breaks. Keep tables as best-effort text. Output ONLY "
                    "the transcribed text with no commentary. Never invent text."
                ),
                "images": [image_b64],
            }],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_ctx": OCR_VLM_NUM_CTX},
        }
        request = urllib.request.Request(
            OCR_VLM_BASE_URL.rstrip("/") + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = ((payload.get("message") or {}).get("content") or "").strip()
        if text:
            return text, "qwen3-vl-ocr", {"model": OCR_VLM_MODEL}
        return None, None, {"why": "qwen3-vl returned no text"}
    except (socket.timeout, TimeoutError) as e:
        return None, None, {"why": "vlm ocr timeout", "timeout": True}
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            return None, None, {"why": "vlm ocr timeout", "timeout": True}
        reason = str(e) or e.__class__.__name__
        return None, None, {"why": reason[-400:]}


STICKY_NOTE_SCHEMA = {
    "type": "object",
    "required": ["notes"],
    "additionalProperties": False,
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "bbox", "color", "content", "confidence", "languages"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "color": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "languages": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

STICKY_CONTENT_SCHEMA = {
    "type": "object",
    "required": ["assignments"],
    "additionalProperties": False,
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "content", "color", "confidence", "languages"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "color": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "languages": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

STICKY_FRAGMENT_SCHEMA = {
    "type": "object",
    "required": ["fragments"],
    "additionalProperties": False,
    "properties": {
        "fragments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "bbox", "color", "content", "confidence", "languages"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "color": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "languages": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

STICKY_DISCOVERY_SCHEMA = {
    "type": "object", "required": ["sheets"], "additionalProperties": False,
    "properties": {"sheets": {"type": "array", "maxItems": NOTE_MAX_REGIONS, "items": {
        "type": "object", "required": ["id", "bbox", "color"], "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "color": {"type": "string"},
        },
    }}}
}

STICKY_VERIFICATION_SCHEMA = {
    "type": "object", "required": ["operations"], "additionalProperties": False,
    "properties": {"operations": {"type": "array", "maxItems": NOTE_MAX_REGIONS * 2, "items": {
        "type": "object", "required": ["op", "ids", "content", "confidence", "languages"], "additionalProperties": False,
        "properties": {
            "op": {"type": "string", "enum": ["keep", "merge", "split", "drop"]},
            "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": NOTE_MAX_REGIONS},
            "content": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "languages": {"type": "array", "items": {"type": "string"}},
        },
    }}}
}

STICKY_MULTICROP_SCHEMA = {
    "type": "object", "required": ["notes"], "additionalProperties": False,
      "properties": {"notes": {"type": "array", "minItems": 0, "maxItems": 1, "items": {
          "type": "object", "required": ["content", "confidence", "languages"], "additionalProperties": False,
          "properties": {
            "content": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "languages": {"type": "array", "items": {"type": "string"}},
        },
    }}}
}


def _strict_json_message(payload):
    content = ((payload.get("message") or {}).get("content") or "") if isinstance(payload, dict) else ""
    return json.loads(content) if isinstance(content, str) else content


def _validate_sticky_discovery(payload):
    if not isinstance(payload, dict) or set(payload) != {"sheets"} or not isinstance(payload["sheets"], list):
        return None
    result = []
    ids = set()
    for item in payload["sheets"]:
        if not isinstance(item, dict) or set(item) != {"id", "bbox", "color"}:
            return None
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in ids:
            return None
        bbox = item["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox):
            return None
        ids.add(item["id"])
        result.append({"id": item["id"], "bbox": [float(v) for v in bbox], "color": item["color"]})
    return {"sheets": result}


def _validate_sticky_verification(payload, valid_ids):
    if not isinstance(payload, dict) or set(payload) != {"operations"} or not isinstance(payload["operations"], list):
        return None
    result = []
    used_ids = set()
    for item in payload["operations"]:
        if not isinstance(item, dict) or set(item) != {"op", "ids", "content", "confidence", "languages"}:
            return None
        op, ids = item["op"], item["ids"]
        if op not in {"keep", "merge", "split", "drop"} or not isinstance(ids, list) or not ids or any(i not in valid_ids for i in ids) or len(set(ids)) != len(ids):
            return None
        if used_ids.intersection(ids):
            return None
        used_ids.update(ids)
        if not isinstance(item["content"], str) or not isinstance(item["confidence"], (int, float)) or isinstance(item["confidence"], bool) or not 0 <= item["confidence"] <= 1 or not isinstance(item["languages"], list) or not all(isinstance(v, str) for v in item["languages"]):
            return None
        if op == "drop" and item["content"]:
            return None
        if op != "drop" and not item["content"].strip():
            return None
        result.append({"op": op, "ids": ids, "content": item["content"].strip(), "confidence": round(float(item["confidence"]), 6), "languages": item["languages"]})
    return {"operations": result}


def _canonicalize_sticky_multicrop(payload):
    if not isinstance(payload, dict) or len(payload) != 1 or not (set(payload) & {"notes", "findings"}):
        return None
    wrapper = "notes" if "notes" in payload else "findings"
    records = payload[wrapper]
    if not isinstance(records, list) or len(records) > 1:
        return None
    canonical = []
    allowed = {"id", "bbox", "box", "bbox_frame", "bbox_shape", "color", "content", "text", "confidence", "languages"}
    for index, item in enumerate(records, 1):
        if not isinstance(item, dict) or not set(item) <= allowed:
            return None
        if ("bbox" in item and "box" in item) or ("content" in item and "text" in item):
            return None
        if "box" not in item and "bbox" not in item or "text" not in item and "content" not in item:
            return None
        item = dict(item)
        item["bbox"] = item.pop("box", item.get("bbox"))
        item["content"] = item.pop("text", item.get("content"))
        if wrapper == "findings":
            item.setdefault("id", f"note-{index}")
            item.setdefault("color", "unknown")
        canonical.append(item)
    return {"notes": canonical}


def _validate_sticky_multicrop(payload, image_size=None, diagnostics=None):
    if not isinstance(payload, dict):
        _validation_failure(diagnostics, "schema", "object")
        return None
    if len(payload) != 1 or set(payload) != {"notes"}:
        _validation_failure(diagnostics, "schema", "wrapper")
        return None
    wrapper = "notes"
    records = payload[wrapper]
    if not isinstance(records, list) or len(records) > 1:
        _validation_failure(diagnostics, f"{wrapper}", "single-record-array")
        return None
    for index, item in enumerate(records, 1):
        if not isinstance(item, dict):
            _validation_failure(diagnostics, f"{wrapper}[{index}]", "object")
            return None
        if set(item) != {"content", "confidence", "languages"}:
            _validation_failure(diagnostics, f"{wrapper}[{index}]", "unknown-field")
            return None
        if not isinstance(item["content"], str) or not item["content"].strip():
            _validation_failure(diagnostics, f"{wrapper}[{index}].content", "nonempty-string")
            return None
        confidence = item["confidence"]
        if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or
                not math.isfinite(confidence) or not 0 <= confidence <= 1):
            _validation_failure(diagnostics, f"{wrapper}[{index}].confidence", "range-0-1")
            return None
        if not isinstance(item["languages"], list) or not all(isinstance(value, str) for value in item["languages"]):
            _validation_failure(diagnostics, f"{wrapper}[{index}].languages", "string-array")
            return None
    return {"findings": [{"content": item["content"].strip(),
                           "confidence": round(float(item["confidence"]), 6),
                           "languages": item["languages"]} for item in records]}


def _multicrop_failure(meta, failure_class, reason):
    meta.update({
        "state": "fallback",
        "failure_class": failure_class,
        "failure_code": MULTICROP_FAILURE_CODES.get(failure_class, "MULTICROP_ERROR"),
        "failure_reason": str(reason)[-400:],
    })
    return meta


def _geometry_evidence(region):
    box = region["box"]
    return {
        "crop_id": region["id"],
        "crop_origin": list(region.get("tile_origin", box[:2])),
        "crop_size": list(region.get("tile_size", [box[2] - box[0], box[3] - box[1]])),
        "source_size": list(region["source_size"]),
        "proposal_box": list(box),
        "provenance": list(region.get("provenance", [])),
    }


def _bounded_response_shape(payload):
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    shape = {"type": "object", "keys": sorted(str(key) for key in payload)}
    for key, value in payload.items():
        if isinstance(value, list):
            item_keys = []
            for item in value[:2]:
                if isinstance(item, dict):
                    item_keys.append(sorted(str(item_key) for item_key in item))
                else:
                    item_keys.append(type(item).__name__)
            shape[str(key)] = {"type": "array", "length": len(value), "item_shapes": item_keys}
        else:
            shape[str(key)] = {"type": type(value).__name__}
    return shape


def _canonicalize_sticky_notes(payload):
    if not isinstance(payload, dict) or set(payload) != {"notes"} or not isinstance(payload["notes"], list):
        return payload
    canonical = []
    allowed = {"id", "bbox", "box", "bbox_frame", "bbox_shape", "color", "content", "text", "confidence", "languages"}
    for index, note in enumerate(payload["notes"], 1):
        if not isinstance(note, dict) or not set(note) <= allowed:
            return payload
        if "bbox" in note and "box" in note or "content" in note and "text" in note:
            return payload
        if "box" not in note or "text" not in note:
            canonical.append(note)
            continue
        item = dict(note)
        item["bbox"] = item.pop("box")
        item["content"] = item.pop("text")
        item.setdefault("id", f"note-{index}")
        item.setdefault("color", "unknown")
        item.setdefault("confidence", 0.0)
        item.setdefault("languages", [])
        canonical.append(item)
    return {"notes": canonical}


def _validation_failure(diagnostics, field, predicate):
    if diagnostics is not None and len(diagnostics) < 24:
        diagnostics.append(f"{field}.{predicate}")


def _validate_sticky_notes(payload, image_size=None, diagnostics=None, require_geometry_contract=False):
    payload = _canonicalize_sticky_notes(payload)
    if not isinstance(payload, dict) or set(payload) != {"notes"} or not isinstance(payload["notes"], list):
        _validation_failure(diagnostics, "schema", "shape")
        return None
    notes = []
    for index, note in enumerate(payload["notes"], 1):
        fields = {"id", "bbox", "color", "content", "confidence", "languages"}
        if require_geometry_contract:
            fields |= {"bbox_frame", "bbox_shape"}
        if not isinstance(note, dict) or set(note) != fields:
            _validation_failure(diagnostics, f"note[{index}]", "fields")
            return None
        bbox = note["bbox"]
        confidence = note["confidence"]
        if require_geometry_contract and note["bbox_frame"] != "normalized":
            _validation_failure(diagnostics, f"note[{index}].bbox_frame", "required-enum")
            return None
        if require_geometry_contract and note["bbox_shape"] != "xyxy":
            _validation_failure(diagnostics, f"note[{index}].bbox_shape", "required-enum")
            return None
        if not isinstance(note["id"], str) or not note["id"]:
            _validation_failure(diagnostics, f"note[{index}].id", "nonempty-string")
            return None
        if not isinstance(note["color"], str) or not note["color"]:
            _validation_failure(diagnostics, f"note[{index}].color", "nonempty-string")
            return None
        if not isinstance(note["content"], str) or not note["content"].strip():
            _validation_failure(diagnostics, f"note[{index}].content", "nonempty-string")
            return None
        if not isinstance(note["languages"], list) or not all(isinstance(value, str) for value in note["languages"]):
            _validation_failure(diagnostics, f"note[{index}].languages", "string-array")
            return None
        if (not isinstance(bbox, list) or len(bbox) != 4 or
                not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in bbox)):
            _validation_failure(diagnostics, f"note[{index}].bbox", "four-numbers")
            return None
        if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or
                not math.isfinite(confidence)):
            _validation_failure(diagnostics, f"note[{index}].confidence", "number")
            return None
        if not 0 <= confidence <= 1:
            _validation_failure(diagnostics, f"note[{index}].confidence", "range-0-1")
            return None
        if any(value < 0 for value in bbox):
            _validation_failure(diagnostics, f"note[{index}].bbox", "nonnegative")
            return None
        frame = note.get("bbox_frame")
        shape = note.get("bbox_shape", "xyxy")
        if require_geometry_contract and frame == "normalized" and shape == "xyxy":
            if any(value > 1 for value in bbox):
                _validation_failure(diagnostics, f"note[{index}].bbox", "normalized-frame-range")
                return None
        elif any(value > 1 for value in bbox):
            if not image_size or len(image_size) != 2 or not all(value > 0 for value in image_size):
                _validation_failure(diagnostics, f"note[{index}].bbox", "pixel-frame-size")
                return None
            width, height = image_size
            if any(value > limit for value, limit in zip(bbox, (width, height, width, height))):
                _validation_failure(diagnostics, f"note[{index}].bbox", "pixel-frame-range")
                return None
            bbox = [bbox[0] / width, bbox[1] / height, bbox[2] / width, bbox[3] / height]
        if not all(0 <= value <= 1 for value in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            _validation_failure(diagnostics, f"note[{index}].bbox", "normalized-nondegenerate")
            return None
        notes.append({"id": note["id"] or f"note-{index}", "bbox": [round(float(value), 6) for value in bbox],
                      "color": note["color"], "content": note["content"],
                      "confidence": round(float(confidence), 6), "languages": note["languages"]})
    return {"notes": notes}


def _validate_sticky_fragments(payload, image_size=None):
    if not isinstance(payload, dict) or set(payload) != {"fragments"} or not isinstance(payload["fragments"], list):
        return None
    notes = _validate_sticky_notes({"notes": payload["fragments"]}, image_size)
    return {"fragments": notes["notes"]} if notes is not None else None


def _run_sticky_vlm(img_path, model, timeout, physical_regions=None):
    if not _vlm_enabled():
        return None, {"model": model, "why": "vlm ocr disabled"}
    try:
        with open(img_path, "rb") as source:
            image_b64 = base64.b64encode(source.read()).decode("ascii")
        regions = physical_regions or _physical_note_regions(img_path)
        proposal_text = json.dumps([
            {"id": f"sheet-{index}", "box": region["box"], "color": region.get("color", "unknown")}
            for index, region in enumerate(regions, 1)
        ], separators=(",", ":"))
        prompt = (
            "Assign content to the supplied physical sheet proposals. Geometry is authoritative: use only proposal ids, "
            "never output coordinates, never split one proposal, and never invent or merge proposals. "
            "Return one assignment at most per proposal; omit blank or non-note proposals. Exclude background, signs, logos, "
            "labels, packaging, and decorative text. Transcribe Traditional Chinese and English faithfully, preserving line breaks. "
            f"Proposals (pixel boxes, not output geometry): {proposal_text} "
            "Return ONLY JSON matching {\"assignments\":[{\"id\":string,\"content\":string,\"color\":string,"
            "\"confidence\":number,\"languages\":[string]}]}. confidence is 0..1."
        )
        started = time.monotonic()
        payload = _ollama_json(
            OCR_VLM_BASE_URL.rstrip("/") + "/api/chat",
            {"model": model, "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
             "stream": False, "think": False, "format": STICKY_CONTENT_SCHEMA,
             "options": {"temperature": 0, "num_ctx": OCR_VLM_NUM_CTX}},
            max(0.1, timeout),
        )
        content = ((payload.get("message") or {}).get("content") or "")
        parsed = json.loads(content) if isinstance(content, str) else content
        assignments = parsed.get("assignments") if isinstance(parsed, dict) else None
        valid_ids = {f"sheet-{index}" for index in range(1, len(regions) + 1)}
        if not isinstance(assignments, list):
            return None, {"model": model, "why": "invalid content-assignment schema", "latency_ms": round((time.monotonic() - started) * 1000)}
        clean = []
        for item in assignments:
            if not isinstance(item, dict) or set(item) != {"id", "content", "color", "confidence", "languages"}:
                return None, {"model": model, "why": "invalid content-assignment item", "latency_ms": round((time.monotonic() - started) * 1000)}
            if item["id"] not in valid_ids or not isinstance(item["content"], str) or not item["content"].strip():
                continue
            if not isinstance(item["confidence"], (int, float)) or not 0 <= item["confidence"] <= 1 or not isinstance(item["languages"], list):
                return None, {"model": model, "why": "invalid content-assignment values", "latency_ms": round((time.monotonic() - started) * 1000)}
            clean.append({**item, "content": item["content"].strip()})
        return {"assignments": clean}, {"model": model, "latency_ms": round((time.monotonic() - started) * 1000), "request": True}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as error:
        return None, {"model": model, "why": str(error)[-400:], "timeout": isinstance(error, (TimeoutError, socket.timeout))}
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        if isinstance(e, urllib.error.HTTPError):
            reason = f"HTTP {e.code}: {e.reason}"
        return None, {"model": model, "why": reason[-400:]}


def _run_sticky_fragment_vlm(img_path, model, timeout):
    """Return approximate content fragments; local geometry owns final sheets."""
    if not _vlm_enabled():
        return None, {"model": model, "why": "vlm ocr disabled"}
    if not _local_url(OCR_VLM_BASE_URL):
        return None, {"model": model, "why": "remote inference disabled"}
    started = time.monotonic()
    try:
        with open(img_path, "rb") as source:
            image_b64 = base64.b64encode(source.read()).decode("ascii")
        prompt = (
            "Find visible sticky-note content. Return approximate fragment boxes only as seeds for local refinement. "
            "Do not include background, signs, logos, packaging, or decorative text. Do not split one physical sheet "
            "unless text is visibly separate. Preserve Traditional Chinese and English faithfully. "
            "Return ONLY JSON matching {\"fragments\":[{\"id\":string,\"bbox\":[x1,y1,x2,y2],"
            "\"color\":string,\"content\":string,\"confidence\":number,\"languages\":[string]}]}. "
            "bbox may be normalized 0..1 or pixels; it is never final output geometry."
        )
        payload = _ollama_json(
            OCR_VLM_BASE_URL.rstrip("/") + "/api/chat",
            {"model": model, "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
             "stream": False, "think": False, "format": STICKY_FRAGMENT_SCHEMA,
             "options": {"temperature": 0, "num_ctx": OCR_VLM_NUM_CTX}},
            max(0.1, timeout),
        )
        content = ((payload.get("message") or {}).get("content") or "")
        parsed = json.loads(content) if isinstance(content, str) else content
        from PIL import Image  # type: ignore
        with Image.open(img_path) as image:
            fragments = _validate_sticky_fragments(parsed, image.size)
        if fragments is None:
            return None, {"model": model, "why": "invalid fragment-seed schema", "latency_ms": round((time.monotonic() - started) * 1000)}
        return fragments, {"model": model, "latency_ms": round((time.monotonic() - started) * 1000), "request": True}
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as error:
        return None, {"model": model, "why": str(error)[-400:], "timeout": isinstance(error, (TimeoutError, socket.timeout))}


def _run_sticky_resident_request(img_path, prompt, schema, timeout):
    """One bounded Ollama request. Caller owns state transitions and fallback."""
    started = time.monotonic()
    with open(img_path, "rb") as source:
        image_b64 = base64.b64encode(source.read()).decode("ascii")
    payload = _ollama_json(
        OCR_VLM_BASE_URL.rstrip("/") + "/api/chat",
        {"model": OCR_VLM_MODEL, "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
         "stream": False, "think": False, "format": schema, "keep_alive": STICKY_RESIDENT_KEEP_ALIVE,
         "options": {"temperature": 0, "num_ctx": OCR_VLM_NUM_CTX}},
        max(0.1, timeout),
    )
    parsed = _strict_json_message(payload)
    meta = {"model": OCR_VLM_MODEL, "latency_ms": round((time.monotonic() - started) * 1000), "request": True,
            "keep_alive": STICKY_RESIDENT_KEEP_ALIVE}
    for key in ("load_duration", "prompt_eval_duration", "eval_duration", "total_duration"):
        if key in payload:
            meta[key] = payload[key]
    return parsed, meta


def _multicrop_regions(path):
    regions = _physical_note_regions(path, max_regions=max(1, STICKY_MULTICROP_MAX_REQUESTS))
    bounded = regions[:max(1, STICKY_MULTICROP_MAX_REQUESTS)]
    return [
        {
            **region,
            "id": region.get("id", f"proposal-{index}"),
            "provenance": list(dict.fromkeys([*region.get("provenance", []), "multicrop"])),
        }
        for index, region in enumerate(bounded, 1)
    ]


def _multicrop_box_iou(left, right):
    left_area = max(0, left[2] - left[0]) * max(0, left[3] - left[1])
    right_area = max(0, right[2] - right[0]) * max(0, right[3] - right[1])
    intersection = max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(0, min(left[3], right[3]) - max(left[1], right[1]))
    union = left_area + right_area - intersection
    return intersection / union if union else 0


def _merge_multicrop_findings(findings):
    merged = []
    for item in sorted(findings, key=lambda value: (-value["confidence"], value["box"][1], value["box"][0], value["content"])):
        normalized = " ".join(item["content"].split()).casefold()
        duplicate = next((existing for existing in merged if existing["normalized"] == normalized and _multicrop_box_iou(existing["box"], item["box"]) >= 0.20), None)
        if duplicate:
            duplicate["provenance"] = list(dict.fromkeys([*duplicate["provenance"], *item["provenance"], "dedupe-merge"]))
            duplicate["candidate_ids"] = list(dict.fromkeys([*duplicate["candidate_ids"], item["candidate_id"]]))
            continue
        merged.append({**item, "normalized": normalized, "candidate_ids": [item["candidate_id"]]})
    result = []
    for index, item in enumerate(sorted(merged, key=lambda value: (value["box"][1], value["box"][0], value["content"])), 1):
        box = item["box"]
        width, height = item["image_size"]
        result.append({"id": item["candidate_id"], "bbox": [round(box[0] / width, 6), round(box[1] / height, 6), round(box[2] / width, 6), round(box[3] / height, 6)],
                       "color": item["color"], "content": item["content"], "confidence": item["confidence"], "languages": item["languages"]})
    return _validate_sticky_notes({"notes": result}), [
        {"id": item["candidate_id"], "box": item["box"], "color": item["color"],
         "provenance": item["provenance"], "candidate_ids": item["candidate_ids"]}
        for item in merged
    ]


def _extract_sticky_notes_multicrop(path, timeout, conversion_meta):
    if not _sticky_multicrop_enabled():
        return None
    from PIL import Image  # type: ignore

    with Image.open(path) as image:
        image_size = image.size
    regions = _multicrop_regions(path)
    if not regions:
        return None
    deadline = time.monotonic() + timeout
    findings = []
    request_meta = {"request_count": 0, "state": "multicrop", "route": "multicrop", "model": OCR_VLM_MODEL, "conversion": conversion_meta,
                    "caps": {"max_requests": STICKY_MULTICROP_MAX_REQUESTS, "crop_count": len(regions)},
                    "privacy": "localhost-only", "geometry_authority": "local"}
    try:
        with tempfile.TemporaryDirectory(prefix="oc-note-multicrop-") as workdir:
            source = Image.open(path).convert("RGB")
            for region in regions:
                if time.monotonic() >= deadline:
                    _multicrop_failure(request_meta, "deadline", "multicrop deadline exceeded")
                    return None, request_meta
                crop_path = os.path.join(workdir, f"{region['id']}.png")
                try:
                    source.crop(tuple(region["box"])).save(crop_path, format="PNG")
                except (OSError, ValueError, TypeError) as error:
                    _multicrop_failure(request_meta, "crop", error)
                    return None, request_meta
                crop_box = region["box"]
                crop_width, crop_height = crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]
                prompt = (f'Read only the visible sticky-note content in this {crop_width}x{crop_height} pixel crop. Return strict JSON '
                           '{"notes":[{"content":string,"confidence":number,"languages":[string]}]}. '
                           'Return content only. Do not output coordinates, ids, colors, or any other fields. '
                          'Return an empty notes array when no readable physical note is present.')
                if time.monotonic() >= deadline:
                    _multicrop_failure(request_meta, "deadline", "multicrop deadline exceeded")
                    return None, request_meta
                request_meta["request_count"] += 1
                request_meta["last_attempt"] = region["id"]
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    response, metadata = _run_sticky_resident_request(crop_path, prompt, STICKY_MULTICROP_SCHEMA,
                                                                       remaining)
                except (TimeoutError, socket.timeout) as error:
                    _multicrop_failure(request_meta, "deadline", str(error) or "multicrop request deadline exceeded")
                    return None, request_meta
                except (OSError, urllib.error.URLError) as error:
                    _multicrop_failure(request_meta, "request-exception", error)
                    return None, request_meta
                schema_diagnostics = []
                clean = _validate_sticky_multicrop(response, (crop_width, crop_height), schema_diagnostics)
                if clean is None:
                    request_meta["response_shape"] = _bounded_response_shape(response)
                    request_meta["failure_predicates"] = schema_diagnostics[:24]
                    request_meta["geometry_evidence"] = _geometry_evidence(region)
                    _multicrop_failure(request_meta, "schema", "invalid multicrop schema")
                    return None, request_meta
                for finding in clean["findings"]:
                    crop_box = region["box"]
                    findings.append({**finding, "box": crop_box, "candidate_id": region["id"],
                                     "color": region.get("color", "unknown"), "image_size": image_size,
                                     "provenance": [*region["provenance"], region["id"]], "request": metadata})
        notes, mappings = _merge_multicrop_findings(findings)
        if notes is None:
            raise ValueError("invalid merged multicrop result")
        request_meta.update({"state": "multicrop-complete", "note_count": len(notes["notes"]), "structured": True,
                             "candidate_mapping": mappings})
        return json.dumps(notes, ensure_ascii=False, separators=(",", ":")), "qwen2.5vl-resident-multicrop", request_meta
    except (OSError, urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError, TypeError, ValueError) as error:
        _multicrop_failure(request_meta, "error", error)
        return None, request_meta


def _local_sticky_geometry(regions, ids):
    return {f"sheet-{index}": dict(region) for index, region in enumerate(regions, 1) if f"sheet-{index}" in ids}


def _apply_sticky_operations(path, operations, regions, image_size):
    """Apply model decisions to local boxes; model cannot create final coordinates."""
    by_id = _local_sticky_geometry(regions, {item for op in operations for item in op["ids"]})
    output = []
    used = set()
    for operation in operations:
        ids = operation["ids"]
        if operation["op"] == "drop":
            used.update(ids)
            continue
        selected = [by_id[item] for item in ids if item in by_id]
        if not selected:
            continue
        box = [min(item["box"][0] for item in selected), min(item["box"][1] for item in selected),
               max(item["box"][2] for item in selected), max(item["box"][3] for item in selected)]
        local_regions = [{**selected[0], "box": box}]
        if operation["op"] == "split":
            local_regions = _split_oversized_regions(path, local_regions)
        for region in local_regions:
            content = operation["content"]
            if operation["op"] == "split" and len(local_regions) > 1:
                lines = [line for line in content.splitlines() if line.strip()]
                index = len(output) % len(local_regions)
                content = lines[index] if index < len(lines) else content
            output.append({"box": region["box"], "color": selected[0].get("color", "unknown"),
                           "content": content, "confidence": operation["confidence"], "languages": operation["languages"]})
        used.update(ids)
    width, height = image_size
    notes = []
    for index, item in enumerate(sorted(output, key=lambda value: (value["box"][1], value["box"][0])), 1):
        box = item["box"]
        notes.append({"id": f"note-{index}", "bbox": [round(box[0] / width, 6), round(box[1] / height, 6), round(box[2] / width, 6), round(box[3] / height, 6)],
                      "color": item["color"], "content": item["content"], "confidence": item["confidence"], "languages": item["languages"]})
    return _validate_sticky_notes({"notes": notes})


def _extract_sticky_notes_resident(path, timeout, conversion_meta):
    if not _sticky_resident_enabled():
        return None
    from PIL import Image  # type: ignore
    with Image.open(path) as image:
        image_size = image.size
    deadline = time.monotonic() + timeout
    physical_regions = _physical_note_regions(path, max(0.1, deadline - time.monotonic()))
    if not physical_regions:
        return None
    indexed = [{"id": f"sheet-{index}", "color": region.get("color", "unknown")} for index, region in enumerate(physical_regions, 1)]
    candidate_ids = [item["id"] for item in indexed]
    discovery_prompt = ("Discover which indexed proposals are physical sticky sheets. Return only JSON matching "
                        '{"sheets":[{"id":string,"bbox":[number,number,number,number],"color":string}]}. '
                        "Use only supplied ids; do not invent ids. Do not include background. Indexed proposals: " + json.dumps(indexed, separators=(",", ":")) )
    request_meta = {
        "request_count": 0,
        "state": "discovery",
        "model": OCR_VLM_MODEL,
        "conversion": conversion_meta,
        "diagnostics": {
            "proposal": {
                "count": len(candidate_ids),
                "ids": candidate_ids,
                "boxes": [region["box"] for region in physical_regions],
                "geometry_authority": "local",
                "bounded": True,
                "privacy": "local-only",
            },
            "discovery": {"candidate_ids": candidate_ids, "privacy": "localhost-only"},
            "verifier": {},
            "operations": {},
            "result": {},
        },
    }
    try:
        discovery, first_meta = _run_sticky_resident_request(path, discovery_prompt, STICKY_DISCOVERY_SCHEMA, max(0.1, deadline - time.monotonic()))
        request_meta.update({"request_count": 1, "discovery": first_meta})
        request_meta["diagnostics"]["discovery"].update({
            "request": first_meta,
            "privacy": "localhost-only",
        })
        clean_discovery = _validate_sticky_discovery(discovery)
        valid_ids = {item["id"] for item in indexed}
        if clean_discovery is None or not set(item["id"] for item in clean_discovery["sheets"]) <= valid_ids:
            raise ValueError("invalid discovery response")
        discovered_ids = [item["id"] for item in clean_discovery["sheets"]]
        request_meta["diagnostics"]["discovery"].update({
            "returned_ids": discovered_ids,
            "returned_count": len(discovered_ids),
            "telemetry_only": True,
        })
        verification_prompt = ("Verify content for indexed sticky sheets. Return only JSON matching "
                               '{"operations":[{"op":"keep|merge|split|drop","ids":[string],"content":string,"confidence":number,"languages":[string]}]}. '
                               "Use only supplied ids. Geometry is local-authoritative: never output or alter coordinates. "
                               "Use merge for one physical sheet represented by multiple ids; split means local geometry must split it. "
                               "Drop background or blank sheets. Indexed sheets: " + json.dumps(candidate_ids, separators=(",", ":")))
        verification, second_meta = _run_sticky_resident_request(path, verification_prompt, STICKY_VERIFICATION_SCHEMA, max(0.1, deadline - time.monotonic()))
        request_meta.update({"request_count": 2, "state": "indexed-verification", "verification": second_meta})
        request_meta["diagnostics"]["verifier"] = {
            "candidate_ids": candidate_ids,
            "discovery_ids_not_gating": True,
            "request": second_meta,
            "privacy": "localhost-only",
            "geometry_authority": "local",
        }
        clean_verification = _validate_sticky_verification(verification, set(candidate_ids))
        if clean_verification is None:
            raise ValueError("invalid verification response")
        notes = _apply_sticky_operations(path, clean_verification["operations"], physical_regions, image_size)
        if notes is None:
            raise ValueError("verification produced invalid geometry")
        request_meta["diagnostics"]["operations"] = {
            "count": len(clean_verification["operations"]),
            "ids": [item for operation in clean_verification["operations"] for item in operation["ids"]],
            "geometry_authority": "local",
            "privacy": "local-only",
        }
        request_meta["diagnostics"]["result"] = {
            "count": len(notes["notes"]),
            "geometry_authority": "local",
            "structured": True,
            "privacy": "local-only",
        }
        return json.dumps(notes, ensure_ascii=False, separators=(",", ":")), "qwen2.5vl-resident-indexed", {
            **request_meta, "route": "resident-indexed", "structured": True, "note_count": len(notes["notes"]), "geometry_authority": "local-authoritative",
            "state_machine": STICKY_RESIDENT_VERSION,
        }
    except (OSError, urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError, TypeError, ValueError) as error:
        request_meta.update({"state": "fallback", "fallback_reason": str(error)[-400:]})
        return None


def _merge_colocated_regions(regions, threshold=0.70):
    """Merge duplicate views of one sheet, preserving grid-adjacent sheets."""
    merged = []
    for region in sorted(regions, key=lambda item: (item["box"][1], item["box"][0])):
        match = next((item for item in merged if _box_iou(item["box"], region["box"]) >= threshold or
                      _overlap_smaller(item["box"], region["box"]) >= threshold), None)
        if match is None:
            merged.append(dict(region))
            continue
        match["box"] = [min(match["box"][0], region["box"][0]), min(match["box"][1], region["box"][1]),
                        max(match["box"][2], region["box"][2]), max(match["box"][3], region["box"][3])]
        match["provenance"] = list(dict.fromkeys([*match.get("provenance", []), *region.get("provenance", []), "colocation-merge"]))
    return merged


def _grow_to_edge_boundaries(path, region):
    """Grow a proposal to the local connected color/edge boundary."""
    from PIL import Image  # type: ignore
    image = Image.open(path).convert("RGB")
    width, height = image.size
    original = [max(0, int(value)) for value in region["box"]]
    box = list(original)
    box[2], box[3] = min(width, int(region["box"][2])), min(height, int(region["box"][3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        return region
    pixels = image.load()
    samples = []
    inset_x = max(1, (box[2] - box[0]) // 5)
    inset_y = max(1, (box[3] - box[1]) // 5)
    for y in range(box[1] + inset_y, box[3] - inset_y, max(1, (box[3] - box[1]) // 8)):
        for x in range(box[0] + inset_x, box[2] - inset_x, max(1, (box[2] - box[0]) // 8)):
            value = pixels[x, y]
            if sum(value) / 3 >= 80:
                samples.append(value)
    if not samples:
        samples = [pixels[(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]]
    sample = tuple(round(sum(value[channel] for value in samples) / len(samples)) for channel in range(3))
    def similar(x, y):
        value = pixels[x, y]
        return sum((value[i] - sample[i]) ** 2 for i in range(3)) ** 0.5 <= 115
    margin_x = max(12, round((original[2] - original[0]) * 1.0))
    margin_y = max(12, round((original[3] - original[1]) * 1.0))
    left_limit, right_limit = max(0, original[0] - margin_x), min(width, original[2] + margin_x)
    top_limit, bottom_limit = max(0, original[1] - margin_y), min(height, original[3] + margin_y)
    while box[0] > left_limit and any(similar(box[0] - 1, y) for y in range(box[1], box[3], max(1, (box[3] - box[1]) // 12))):
        box[0] -= 1
    while box[2] < right_limit and any(similar(box[2], y) for y in range(box[1], box[3], max(1, (box[3] - box[1]) // 12))):
        box[2] += 1
    while box[1] > top_limit and any(similar(x, box[1] - 1) for x in range(box[0], box[2], max(1, (box[2] - box[0]) // 12))):
        box[1] -= 1
    while box[3] < bottom_limit and any(similar(x, box[3]) for x in range(box[0], box[2], max(1, (box[2] - box[0]) // 12))):
        box[3] += 1
    return {**region, "box": box, "provenance": [*region.get("provenance", []), "edge-boundary-growth"]}


def _refine_fragment_regions(path, fragments, physical_regions, image_size):
    """Use VLM boxes only as seeds; return locally grown physical-sheet regions."""
    width, height = image_size
    seeds = []
    for fragment in fragments:
        bbox = fragment.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        values = [float(value) for value in bbox]
        if max(values) <= 1:
            values = [values[0] * width, values[1] * height, values[2] * width, values[3] * height]
        if values[2] <= values[0] or values[3] <= values[1]:
            continue
        seeds.append({"box": [round(value) for value in values], "color": fragment.get("color", "unknown"),
                      "provenance": ["fragment-seed"]})
    # Fragment boxes are the only content-model geometry. Physical detectors
    # may reject obvious outliers, but must not replace a valid seed with a
    # larger unrelated rectangle.
    refined = [_grow_to_edge_boundaries(path, seed) for seed in seeds]
    return _merge_colocated_regions(refined)


def _assign_fragments_to_regions(fragments, regions, image_size):
    width, height = image_size
    assigned = {}
    for fragment in fragments:
        bbox = fragment.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        values = [float(value) for value in bbox]
        fragment_box = values if max(values) > 1 else [values[0] * width, values[1] * height, values[2] * width, values[3] * height]
        ranked = [(max(_box_iou(fragment_box, region["box"]), _overlap_smaller(fragment_box, region["box"])), index)
                  for index, region in enumerate(regions)]
        if ranked:
            score, index = max(ranked)
            if score >= 0.20:
                assigned.setdefault(index, []).append(fragment)
    return assigned


def _run_tesseract(img_path, timeout, psm=None):
    command = ["tesseract", img_path, "stdout"]
    if psm is not None:
        command.extend(["--psm", str(psm)])
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _run_apple_vision_ocr(img_path, timeout):
    if sys.platform != "darwin" or not os.path.isfile(APPLE_VISION_OCR_BIN):
        return None, {"why": "Apple Vision bridge unavailable"}
    try:
        proc = subprocess.run(
            [APPLE_VISION_OCR_BIN, img_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, timeout),
        )
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        if proc.returncode != 0 or payload.get("status") != "ok":
            return None, {"why": payload.get("error", "Apple Vision OCR failed")}
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None, {"why": "Apple Vision OCR returned no text"}
        return text.strip(), {
            "confidence": _normalize_ocr_confidence(payload.get("confidence", 0), "apple"),
            "latency_ms": payload.get("latencyMs"),
            "revision": payload.get("revision"),
            "recognition_level": payload.get("recognitionLevel"),
            "language_correction": payload.get("usesLanguageCorrection"),
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError) as error:
        return None, {"why": f"Apple Vision OCR unavailable: {error}"}


def _run_apple_vision_rectangles(img_path, timeout):
    if sys.platform != "darwin" or not os.path.isfile(APPLE_VISION_OCR_BIN):
        return None, {"why": "Apple Vision rectangle bridge unavailable"}
    try:
        proc = subprocess.run(
            [APPLE_VISION_OCR_BIN, img_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, timeout),
        )
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        if proc.returncode != 0 or payload.get("status") != "ok":
            return None, {"why": payload.get("error", "Apple Vision rectangle detection failed")}
        detections = payload.get("detections")
        if not isinstance(detections, list):
            return None, {"why": "Apple Vision rectangle response missing detections"}
        return detections, {
            "latency_ms": payload.get("latencyMs"),
            "revision": payload.get("revision"),
            "image_size": payload.get("imageSize"),
            "bridge": "apple-vision",
        }
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError) as error:
        return None, {"why": f"Apple Vision rectangle detection unavailable: {error}"}


def _document_image(path, timeout=5.0):
    """Classify paper-like images with deterministic visual and OCR layout signals."""
    try:
        from PIL import Image, ImageStat  # type: ignore
        image = Image.open(path).convert("RGB")
        width, height = image.size
        if min(width, height) < 600:
            return False, {"reason": "small-image", "size": [width, height]}
        ratio = width / height
        shape = 0.45 <= ratio <= 2.2
        sample = image.resize((64, 64))
        stats = ImageStat.Stat(sample)
        mean = sum(stats.mean) / 3
        spread = sum(stats.stddev) / 3
        pixels = list(sample.getdata())
        bright = sum(1 for r, g, b in pixels if (r + g + b) / 3 >= 190) / len(pixels)
        saturation = sum(max(p) - min(p) for p in pixels) / (len(pixels) * 255)
        features = {"size": [width, height], "aspect_ratio": ratio, "bright_fraction": bright, "saturation": saturation, "mean": mean, "spread": spread}
        text_signal = {"word_count": 0, "line_count": 0, "coverage": 0.0}
        if which("tesseract") and timeout > 0:
            remaining = max(0.1, timeout)
            try:
                rc, tsv, _ = _run_tesseract_tsv(path, remaining, psm=11)
                if rc == 0:
                    words = []
                    lines = set()
                    min_left, min_top = width, height
                    max_right, max_bottom = 0, 0
                    for raw in tsv.splitlines()[1:]:
                        fields = raw.split("\t")
                        if len(fields) < 12 or not fields[11].strip():
                            continue
                        try:
                            left, top = int(fields[6]), int(fields[7])
                            word_width, word_height = int(fields[8]), int(fields[9])
                        except ValueError:
                            continue
                        words.append(fields[11].strip())
                        lines.add(tuple(fields[:5]))
                        min_left, min_top = min(min_left, left), min(min_top, top)
                        max_right, max_bottom = max(max_right, left + word_width), max(max_bottom, top + word_height)
                    text_signal = {
                        "word_count": len(words),
                        "line_count": len(lines),
                        "coverage": ((max_right - min_left) * (max_bottom - min_top) / (width * height)) if words else 0.0,
                    }
            except (OSError, subprocess.TimeoutExpired):
                text_signal["timeout"] = True
        features["text_signal"] = text_signal
        visual_signal = shape and bright >= 0.45 and saturation <= 0.28 and mean >= 120 and spread >= 0.5
        text_layout_signal = text_signal["word_count"] >= 2 and text_signal["line_count"] >= 2 and text_signal["coverage"] >= 0.0005
        document = visual_signal and text_layout_signal
        features["visual_signal"] = visual_signal
        features["text_layout_signal"] = text_layout_signal
        return document, features
    except Exception as error:
        return False, {"reason": f"classification-unavailable: {error}"}


def _run_tesseract_tsv(img_path, timeout, psm=11):
    proc = subprocess.run(
        ["tesseract", img_path, "stdout", "--psm", str(psm), "tsv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _ocr_confidence(tsv):
    values = []
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 12 or not fields[11].strip():
            continue
        try:
            confidence = float(fields[10])
            if confidence >= 0:
                values.append(confidence)
        except ValueError:
            continue
    return _normalize_ocr_confidence(sum(values) / len(values) if values else 0.0, "tesseract")


def _normalize_ocr_confidence(value, engine):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if engine == "apple":
        confidence *= 100.0
    return min(max(confidence, 0.0), 100.0)


def _ocr_text(tsv):
    lines = {}
    for raw in tsv.splitlines()[1:]:
        fields = raw.split("\t")
        if len(fields) < 12 or not fields[-1].strip():
            continue
        key = tuple(fields[:5])
        try:
            left = int(fields[6] or 0)
            top = int(fields[7] or 0)
            word_left = left
        except ValueError:
            left = top = word_left = 0
        lines.setdefault(key, {"top": top, "left": left, "words": []})["words"].append(
            (word_left, fields[-1].strip())
        )
    ordered_lines = sorted(lines.values(), key=lambda line: (line["top"], line["left"]))
    return "\n".join(
        " ".join(text for _, text in sorted(line["words"], key=lambda word: word[0]))
        for line in ordered_lines
    )


def _ocr_candidate(path, timeout, psm=11):
    rc, tsv, _ = _run_tesseract_tsv(path, timeout, psm=psm)
    if rc != 0:
        return "", 0.0
    text = _ocr_text(tsv)
    return text, _ocr_confidence(tsv)


def _image_derivative(path, workdir, angle=0, threshold=False):
    """Create a disposable upright OCR/VLM derivative; never mutate source."""
    from PIL import Image, ImageOps, ImageFilter  # type: ignore

    image = ImageOps.exif_transpose(Image.open(path)).convert("L")
    if angle:
        image = image.rotate(-angle, expand=True)
    min_dimension = min(image.size)
    upscaled = min_dimension < 1200
    if upscaled:
        scale = 2 if min_dimension < 600 else 1.5
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    image = image.filter(ImageFilter.SHARPEN)
    if threshold:
        image = image.point(lambda pixel: 255 if pixel >= 180 else 0)
    output = os.path.join(workdir, f"image-{angle}-{'threshold' if threshold else 'gray'}.png")
    image.save(output, format="PNG")
    return output, {"angle": angle, "threshold": threshold, "upscaled": upscaled, "size": list(image.size)}


def _sips_dimensions(path, timeout):
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(0.1, timeout),
    )
    values = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if line.strip().startswith(("pixelWidth:", "pixelHeight:")):
            try:
                values.append(int(line.split(":", 1)[1].strip()))
            except ValueError:
                pass
    if proc.returncode != 0 or len(values) != 2:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-300:].strip() or "sips could not read image dimensions")
    return values[0], values[1]


def _prepare_image_input(path, workdir, timeout):
    """Return Pillow-readable image path; source remains untouched."""
    is_heic = os.path.splitext(path)[1].lower() in HEIC_EXT
    try:
        from PIL import Image  # type: ignore
        with Image.open(path):
            return path, {"converted": False}
    except Exception as pillow_error:
        if not is_heic:
            return path, {"converted": False}
        if sys.platform != "darwin" or not which("sips"):
            raise RuntimeError("Pillow cannot read HEIC and macOS sips is unavailable")
        width, height = _sips_dimensions(path, timeout)
        pixels = width * height
        output = os.path.join(workdir, "source.png")
        command = ["sips", "-s", "format", "png"]
        if pixels > IMAGE_MAX_PIXELS:
            command.extend(["--resampleHeightWidthMax", str(max(1, int(IMAGE_MAX_PIXELS ** 0.5)) )])
        command.extend(["--out", output, path])
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(0.1, timeout))
        if proc.returncode != 0 or not os.path.isfile(output):
            detail = proc.stderr.decode("utf-8", errors="replace")[-300:].strip()
            raise RuntimeError(f"sips HEIC conversion failed{': ' + detail if detail else ''}")
        return output, {"converted": True, "converter": "sips", "source_dimensions": [width, height], "max_pixels": IMAGE_MAX_PIXELS, "resampled": pixels > IMAGE_MAX_PIXELS}


def _note_color(rgb):
    r, g, b = rgb
    names = {
        "red": (0.85, 0.18, 0.18), "orange": (0.9, 0.45, 0.12),
        "yellow": (0.9, 0.78, 0.12), "green": (0.2, 0.65, 0.25),
        "cyan": (0.12, 0.68, 0.72), "blue": (0.18, 0.38, 0.85),
        "purple": (0.55, 0.25, 0.72), "pink": (0.88, 0.35, 0.58),
    }
    scale = max(r + g + b, 1)
    chroma = (max(rgb) - min(rgb)) / scale
    if chroma < 0.12:
        return "neutral"
    best = min(names, key=lambda name: sum((rgb[i] / 255 - names[name][i]) ** 2 for i in range(3)))
    return best


def _note_regions(path, workdir):
    """Find large saturated paper-like connected regions without scene-specific coordinates."""
    from PIL import Image, ImageFilter, ImageStat  # type: ignore

    image = Image.open(path).convert("RGB")
    width, height = image.size
    scale = min(1.0, 512 / max(width, height))
    small = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    sw, sh = small.size
    pixels = list(small.getdata())
    mask = bytearray(sw * sh)
    for index, (r, g, b) in enumerate(pixels):
        value = (r + g + b) / 3
        chroma = (max(r, g, b) - min(r, g, b)) / 255
        mask[index] = 1 if value >= 100 and chroma >= 0.08 else 0

    # Close text/shadow holes before connected-component extraction. Sampling is
    # bounded, so this cannot expand work with source resolution.
    mask_image = Image.frombytes("L", (sw, sh), bytes(255 if value else 0 for value in mask))
    mask_image = mask_image.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    mask = bytearray(1 if value >= 128 else 0 for value in mask_image.getdata())

    seen = bytearray(sw * sh)
    regions = []
    for start in range(sw * sh):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack = [start]
        points = []
        color_sum = [0, 0, 0]
        min_x = max_x = start % sw
        min_y = max_y = start // sw
        while stack:
            current = stack.pop()
            x, y = current % sw, current // sw
            points.append(current)
            for channel, value in enumerate(pixels[current]):
                color_sum[channel] += value
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < sw and 0 <= ny < sh:
                    neighbor = ny * sw + nx
                    color_distance = sum((pixels[current][channel] - pixels[neighbor][channel]) ** 2 for channel in range(3)) ** 0.5
                    if mask[neighbor] and color_distance <= 90 and not seen[neighbor]:
                        seen[neighbor] = 1
                        stack.append(neighbor)
        box_area = (max_x - min_x + 1) * (max_y - min_y + 1)
        image_area = sw * sh
        fill = len(points) / box_area
        fraction = box_area / image_area
        if len(points) < 80 or fraction < 0.01 or fraction > 0.60 or fill < 0.45:
            continue
        # Reject thin colorful background bands; notes have a bounded paper-like aspect.
        aspect = (max_x - min_x + 1) / max(1, max_y - min_y + 1)
        if aspect < 0.18 or aspect > 5.5:
            continue
        crop_box = (
            max(0, round(min_x / scale) - round(width * 0.01)),
            max(0, round(min_y / scale) - round(height * 0.01)),
            min(width, round((max_x + 1) / scale) + round(width * 0.01)),
            min(height, round((max_y + 1) / scale) + round(height * 0.01)),
        )
        mean = [value / len(points) for value in color_sum]
        mean_value = sum(mean) / 3
        colorful = (max(mean) - min(mean)) / max(mean_value, 1)
        if colorful < NOTE_MIN_COLORFULNESS or mean_value < 100:
            continue
        regions.append({
            "box": list(crop_box),
            "position": {
                "x": round(crop_box[0] / width, 4), "y": round(crop_box[1] / height, 4),
                "width": round((crop_box[2] - crop_box[0]) / width, 4),
                "height": round((crop_box[3] - crop_box[1]) / height, 4),
            },
            "color": _note_color(mean),
            "colorfulness": round(colorful, 4),
            "area_fraction": round(fraction, 5),
            "fill": round(fill, 4),
        })
    regions.sort(key=lambda item: (round(item["box"][1] / max(1, height * 0.05)), item["box"][0]))
    return regions[:NOTE_MAX_REGIONS], {"size": [width, height], "sample_size": [sw, sh], "candidate_count": len(regions)}


def _edge_regions(path):
    """Find bounded rectangular edge clusters as a weak proposal source."""
    from PIL import Image, ImageFilter  # type: ignore

    image = Image.open(path).convert("L")
    width, height = image.size
    scale = min(1.0, 256 / max(width, height))
    small = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    edges = small.filter(ImageFilter.FIND_EDGES)
    pixels = list(edges.getdata())
    sw, sh = small.size
    mask = bytearray(value >= 42 for value in pixels)
    seen = bytearray(sw * sh)
    proposals = []
    for start in range(sw * sh):
        if not mask[start] or seen[start]:
            continue
        seen[start] = 1
        stack = [start]
        points = []
        min_x = max_x = start % sw
        min_y = max_y = start // sw
        while stack:
            current = stack.pop()
            x, y = current % sw, current // sw
            points.append(current)
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < sw and 0 <= ny < sh:
                    neighbor = ny * sw + nx
                    if mask[neighbor] and not seen[neighbor]:
                        seen[neighbor] = 1
                        stack.append(neighbor)
        box_width, box_height = max_x - min_x + 1, max_y - min_y + 1
        area = box_width * box_height
        fraction = area / (sw * sh)
        fill = len(points) / area
        aspect = box_width / max(box_height, 1)
        if len(points) < 20 or fraction < 0.01 or fraction > 0.60 or fill < 0.08 or not 0.18 <= aspect <= 5.5:
            continue
        box = [
            max(0, round(min_x / scale)), max(0, round(min_y / scale)),
            min(width, round((max_x + 1) / scale)), min(height, round((max_y + 1) / scale)),
        ]
        proposals.append({
            "box": box,
            "position": {"x": box[0] / width, "y": box[1] / height, "width": (box[2] - box[0]) / width, "height": (box[3] - box[1]) / height},
            "color": "neutral", "colorfulness": 0.0, "edge_density": round(fill, 4),
            "area_fraction": round(fraction, 5), "fill": round(fill, 4), "provenance": ["edge"],
        })
    return proposals


def _physical_note_regions(path, timeout=30.0, max_regions=None):
    """Build bounded geometry proposals without content-model geometry."""
    from PIL import Image, ImageFilter, ImageStat  # type: ignore

    deadline = time.monotonic() + max(0.1, timeout)
    image = Image.open(path).convert("L")
    width, height = image.size
    scale = min(1.0, 256 / max(width, height))
    small = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    contrast = small.filter(ImageFilter.FIND_EDGES)
    contrast_pixels = list(contrast.getdata())
    if contrast_pixels:
        contrast_regions = _contrast_regions(path, contrast, scale, width, height)
    else:
        contrast_regions = []
    color_regions, _ = _note_regions(path, tempfile.gettempdir())
    rectangle_regions = []
    if sys.platform == "darwin":
        detections, _ = _run_apple_vision_rectangles(path, 2.0)
        for item in detections or []:
            if not isinstance(item, dict) or not isinstance(item.get("box"), list) or len(item["box"]) != 4:
                continue
            geometry = item.get("geometry") or {}
            rectangle_regions.append({
                "box": item["box"], "color": item.get("color", "neutral"),
                "colorfulness": item.get("colorfulness", 0),
                "rectangle_confidence": geometry.get("rectangleConfidence", 0),
                "area_fraction": geometry.get("areaFraction", 0),
                "provenance": ["rectangle"],
            })
    candidates = color_regions + contrast_regions + _edge_regions(path) + rectangle_regions
    if NOTE_TILED_PROPOSALS and time.monotonic() < deadline:
        candidates += _tiled_note_regions(path, max(0.1, deadline - time.monotonic()))
    normalized = []
    for region in candidates:
        if not isinstance(region.get("box"), list) or len(region["box"]) != 4:
            continue
        if ("rectangle" in region.get("provenance", []) and
                float(region.get("colorfulness") or 0) < 0.08 and
                float(region.get("area_fraction") or 0) < 0.01):
            continue
        item = _globalize_region(region, (width, height))
        item["candidate_score"] = _candidate_score(item)
        if "contrast" in item.get("provenance", []) and item.get("color", "neutral") == "neutral":
            if float(item.get("edge_density") or 0) < 0.08:
                continue
        normalized.append(item)
    selected = _nms_regions(
        _split_oversized_regions(path, normalized),
        max_regions=max_regions if max_regions is not None else (NOTE_TILE_MAX_PROPOSALS if NOTE_TILED_PROPOSALS else NOTE_MAX_REGIONS),
    )
    return selected


def _tile_starts(length, tile_size, overlap):
    if length <= tile_size:
        return [0]
    step = max(1, round(tile_size * (1 - overlap)))
    starts = list(range(0, max(1, length - tile_size + 1), step))
    final = length - tile_size
    if starts[-1] != final:
        starts.append(final)
    return starts


def _tiled_note_regions(path, timeout=30.0):
    """Find Apple Vision, color, and edge proposals in overlapping tiles."""
    from PIL import Image  # type: ignore

    image = Image.open(path).convert("RGB")
    width, height = image.size
    tile_width = min(max(64, NOTE_TILE_SIZE), width)
    tile_height = min(max(64, NOTE_TILE_SIZE), height)
    candidates = []
    rows = _tile_starts(height, tile_height, NOTE_TILE_OVERLAP)
    columns = _tile_starts(width, tile_width, NOTE_TILE_OVERLAP)
    tile_coordinates = [(row, top, column, left) for row, top in enumerate(rows) for column, left in enumerate(columns)]
    if len(tile_coordinates) > NOTE_TILE_MAX_TILES:
        tile_coordinates = tile_coordinates[:NOTE_TILE_MAX_TILES]
    deadline = time.monotonic() + max(0.1, timeout)
    with tempfile.TemporaryDirectory(prefix="oc-note-tiles-") as workdir:
        for row, top, column, left in tile_coordinates:
            if time.monotonic() >= deadline:
                break
            tile_path = os.path.join(workdir, f"tile-{row}-{column}.png")
            image.crop((left, top, left + tile_width, top + tile_height)).save(tile_path)
            color_regions, _ = _note_regions(tile_path, workdir)
            edge_regions = _edge_regions(tile_path)
            vision_regions = []
            if sys.platform == "darwin":
                remaining = max(0.1, deadline - time.monotonic())
                detections, _ = _run_apple_vision_rectangles(tile_path, remaining)
                for detection in detections or []:
                    if not isinstance(detection, dict) or not isinstance(detection.get("box"), list) or len(detection["box"]) != 4:
                        continue
                    geometry = detection.get("geometry") or {}
                    vision_regions.append({
                        "box": detection["box"],
                        "position": detection.get("position"),
                        "color": detection.get("color", "neutral"),
                        "colorfulness": detection.get("colorfulness", 0),
                        "rectangle_confidence": geometry.get("rectangleConfidence", 0),
                        "area_fraction": geometry.get("areaFraction", 0),
                        "text": detection.get("text", ""),
                        "confidence": _normalize_ocr_confidence(detection.get("confidence", 0), "apple"),
                        "ocr_tool": detection.get("ocrTool", "apple-vision"),
                        "provenance": ["rectangle", "tile", "overlap"],
                    })
            for region in [*vision_regions, *color_regions, *edge_regions]:
                item = dict(region)
                item["tile_origin"] = [left, top]
                item["tile_size"] = [tile_width, tile_height]
                item["source_size"] = [tile_width, tile_height]
                item["coordinate_frame"] = "tile"
                item.setdefault("provenance", [])
                item["provenance"] = [*item["provenance"], "tile", "overlap"]
                global_item = _globalize_region(item, (width, height))
                box = global_item.get("box", [])
                if len(box) == 4 and box[2] > box[0] and box[3] > box[1]:
                    candidates.append(global_item)
    return candidates


def _globalize_region(region, image_size):
    """Convert proposal coordinates to source-image pixels, retaining origin."""
    width, height = image_size
    box = region.get("box")
    if not isinstance(box, list) or len(box) != 4:
        return dict(region)
    source_size = region.get("source_size") or image_size
    source_width, source_height = source_size
    frame = region.get("coordinate_frame") or region.get("frame") or "global"
    source_box = list(box)
    if frame in {"normalized", "relative"}:
        box = [box[0] * source_width, box[1] * source_height,
               box[2] * source_width, box[3] * source_height]
    elif frame not in {"global", "pixel"}:
        box = [float(value) for value in box]
    scale_x = 1 if frame == "tile" else (width / source_width if source_width else 1)
    scale_y = 1 if frame == "tile" else (height / source_height if source_height else 1)
    origin = region.get("tile_origin") or region.get("origin") or [0, 0]
    global_box = [
        max(0, min(width, round(box[0] * scale_x + origin[0]))),
        max(0, min(height, round(box[1] * scale_y + origin[1]))),
        max(0, min(width, round(box[2] * scale_x + origin[0]))),
        max(0, min(height, round(box[3] * scale_y + origin[1]))),
    ]
    item = dict(region)
    item["box"] = global_box
    item["coordinate_frame"] = "global"
    item["source_frame"] = frame
    item["source_box"] = source_box
    if origin != [0, 0]:
        item["tile_origin"] = list(origin)
    item["source_size"] = [source_width, source_height]
    item.setdefault("provenance", [])
    item["provenance"] = list(dict.fromkeys([*item["provenance"], frame, "globalized"]))
    return item


def _split_oversized_regions(path, candidates):
    """Split oversized proposals at up to three image-derived edge valleys."""
    from PIL import Image, ImageFilter  # type: ignore

    image = Image.open(path).convert("L")
    output = []
    for candidate in candidates:
        box = candidate["box"]
        width, height = box[2] - box[0], box[3] - box[1]
        if width * height < 0.015 * image.width * image.height:
            output.append(candidate)
            continue
        crop = image.crop(tuple(box)).filter(ImageFilter.FIND_EDGES)
        pixels = list(crop.getdata())
        axis = 0 if width >= height else 1
        span = width if axis == 0 else height
        energy = []
        for position in range(span):
            values = [pixels[row * width + position] for row in range(height)] if axis == 0 else [pixels[position * width + column] for column in range(width)]
            energy.append(sum(values) / max(1, len(values)))
        margin = max(2, round(span * 0.15))
        if len(energy) <= margin * 2:
            output.append(candidate)
            continue
        average = sum(energy) / len(energy)
        valleys = []
        for position in range(margin, len(energy) - margin):
            if energy[position] <= energy[position - 1] and energy[position] <= energy[position + 1]:
                valleys.append(position)
        valleys.sort(key=lambda position: energy[position])
        selected_valleys = []
        minimum_gap = max(8, round(span * 0.12))
        for valley in valleys:
            if energy[valley] > average * 0.8:
                continue
            if any(abs(valley - prior) < minimum_gap for prior in selected_valleys):
                continue
            selected_valleys.append(valley)
            if len(selected_valleys) == 3:
                break
        selected_valleys.sort()
        if not selected_valleys:
            output.append(candidate)
            continue
        boundaries = [0, *selected_valleys, span]
        for start, end in zip(boundaries, boundaries[1:]):
            if end - start < margin:
                continue
            part = list(box)
            if axis == 0:
                part[0], part[2] = box[0] + start, box[0] + end
            else:
                part[1], part[3] = box[1] + start, box[1] + end
            child = dict(candidate)
            child["box"] = part
            child["provenance"] = [*candidate.get("provenance", []), "edge-valley-split", f"split-{axis}"]
            child["parent_box"] = list(box)
            child["area_fraction"] = (part[2] - part[0]) * (part[3] - part[1]) / (image.width * image.height)
            child["candidate_score"] = _candidate_score(child)
            output.append(child)
    return output


def _contrast_regions(path, edge_image, scale, width, height):
    """Find high-contrast connected sheet interiors as pale-sheet proposals."""
    from PIL import ImageFilter  # type: ignore

    mask_image = edge_image.point(lambda value: 255 if value >= 18 else 0)
    mask_image = mask_image.filter(ImageFilter.MaxFilter(11)).filter(ImageFilter.MinFilter(11))
    pixels = list(mask_image.getdata())
    sw, sh = mask_image.size
    seen = bytearray(sw * sh)
    proposals = []
    for start, value in enumerate(pixels):
        if value == 0 or seen[start]:
            continue
        seen[start] = 1
        stack, points = [start], []
        min_x = max_x = start % sw
        min_y = max_y = start // sw
        while stack:
            current = stack.pop()
            x, y = current % sw, current // sw
            points.append(current)
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < sw and 0 <= ny < sh:
                    neighbor = ny * sw + nx
                    if pixels[neighbor] and not seen[neighbor]:
                        seen[neighbor] = 1
                        stack.append(neighbor)
        box_width, box_height = max_x - min_x + 1, max_y - min_y + 1
        area_fraction = box_width * box_height / max(1, sw * sh)
        aspect = box_width / max(1, box_height)
        if len(points) < 30 or not 0.01 <= area_fraction <= 0.60 or not 0.18 <= aspect <= 5.5:
            continue
        box = [max(0, round(min_x / scale)), max(0, round(min_y / scale)),
               min(width, round((max_x + 1) / scale)), min(height, round((max_y + 1) / scale))]
        proposals.append({"box": box, "color": "neutral", "colorfulness": 0.0,
                          "edge_density": min(1.0, len(points) / max(1, box_width * box_height)),
                          "area_fraction": area_fraction, "provenance": ["contrast"]})
    return proposals


def _candidate_score(candidate):
    geometry = candidate.get("geometry") or {}
    colorfulness = min(max(float(candidate.get("colorfulness") or 0), 0), 1)
    rectangle = min(max(float(candidate.get("rectangle_confidence") or geometry.get("rectangleConfidence", 0)), 0), 1)
    text_confidence = min(max(float(candidate.get("confidence") or 0), 0), 100) / 100
    edge_density = min(max(float(candidate.get("edge_density") or 0), 0), 1)
    area = float(candidate.get("area_fraction") or geometry.get("areaFraction", 0))
    size_score = 1.0 if 0.01 <= area <= 0.60 else 0.0
    return round(0.35 * colorfulness + 0.25 * rectangle + 0.20 * text_confidence + 0.10 * min(edge_density * 4, 1) + 0.10 * size_score, 6)


def _nms_regions(candidates, threshold=0.55, max_regions=NOTE_MAX_REGIONS):
    """Deduplicate proposals without collapsing grid-adjacent sheet boxes."""
    def rank(item):
        provenance = set(item.get("provenance", []))
        source_bonus = len(provenance & {"color", "rectangle", "edge", "contrast"}) * 0.001
        tile_bonus = 0.015 if "tile" in provenance else 0
        pale_edge_bonus = 0.02 if "contrast" in provenance and float(item.get("edge_density") or 0) >= 0.08 else 0
        split_bonus = 0.01 if "edge-valley-split" in provenance else 0
        return (item["candidate_score"] + source_bonus + tile_bonus + pale_edge_bonus + split_bonus,
                item.get("confidence", 0), item["box"][1], item["box"][0])
    ranked = sorted(
        (item for item in candidates if item["candidate_score"] >= NOTE_MIN_CANDIDATE_SCORE),
        key=rank,
        reverse=True,
    )
    selected = []
    for candidate in ranked:
        if any((_box_iou(candidate["box"], previous["box"]) >= threshold or _overlap_smaller(candidate["box"], previous["box"]) >= threshold)
               and not _grid_adjacent(candidate["box"], previous["box"])
               for previous in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item["box"][1], item["box"][0]))[:max_regions]


def _grid_adjacent(first, second):
    """True when boxes touch or nearly touch along one axis, not duplicate overlap."""
    horizontal = abs(first[2] - second[0]) <= 2 or abs(second[2] - first[0]) <= 2
    vertical = abs(first[3] - second[1]) <= 2 or abs(second[3] - first[1]) <= 2
    y_overlap = min(first[3], second[3]) - max(first[1], second[1])
    x_overlap = min(first[2], second[2]) - max(first[0], second[0])
    return (horizontal and y_overlap > 0) or (vertical and x_overlap > 0)


def _box_iou(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _overlap_smaller(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    smaller = min(first_area, second_area)
    return intersection / smaller if smaller else 0.0


def _reconcile_sticky_notes(notes, physical_regions, image_size):
    """Keep VLM text only when it maps to one generic physical-sheet region.

    Physical proposals are the independent region authority. Multiple VLM
    records may map to one proposal; their distinct text is merged there.
    """
    if not notes or not image_size:
        return notes
    width, height = image_size
    candidates = [region for region in physical_regions if isinstance(region.get("box"), list) and len(region["box"]) == 4]
    if not candidates:
        return notes

    assigned = {}
    unmatched = []
    for note in notes:
        bbox = note.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        note_box = [bbox[0] * width, bbox[1] * height, bbox[2] * width, bbox[3] * height]
        ranked = []
        for index, candidate in enumerate(candidates):
            candidate_box = candidate["box"]
            overlap = _overlap_smaller(note_box, candidate_box)
            overlap_iou = _box_iou(note_box, candidate_box)
            note_area = max(0, note_box[2] - note_box[0]) * max(0, note_box[3] - note_box[1])
            candidate_area = max(0, candidate_box[2] - candidate_box[0]) * max(0, candidate_box[3] - candidate_box[1])
            area_ratio = note_area / candidate_area if candidate_area else float("inf")
            if (overlap_iou < 0.08 and overlap < 0.45) or area_ratio > 8:
                continue
            ranked.append((overlap_iou + overlap * 0.5 - min(area_ratio, 8) * 0.01, index))
        if not ranked:
            unmatched.append(note)
            continue
        _, candidate_index = max(ranked)
        assigned.setdefault(candidate_index, []).append(note)

    reconciled = []
    for candidate_index, grouped in assigned.items():
        candidate = candidates[candidate_index]
        box = candidate["box"]
        contents = []
        languages = []
        best = max(grouped, key=lambda item: item.get("confidence", 0))
        for note in sorted(grouped, key=lambda item: (item.get("bbox", [0, 0])[1], item.get("bbox", [0, 0])[0])):
            content = str(note.get("content", "")).strip()
            if content and content not in contents:
                contents.append(content)
            for language in note.get("languages", []):
                if language not in languages:
                    languages.append(language)
        if not contents:
            continue
        reconciled.append({
            **best,
            "bbox": [round(box[0] / width, 6), round(box[1] / height, 6),
                     round(box[2] / width, 6), round(box[3] / height, 6)],
            "content": "\n".join(contents),
            "confidence": max(note.get("confidence", 0) for note in grouped),
            "languages": languages,
        })

    # Physical proposals are useful provenance, not a hard recall gate. If
    # they cover only a minority of VLM records, retain otherwise valid VLM
    # records that have no physical match. This prevents an under-recalling
    # detector from erasing structured content while still rejecting weak
    # background guesses and duplicate boxes.
    matched_count = sum(len(grouped) for grouped in assigned.values())
    coverage_weak = matched_count < len(notes) and matched_count * 2 < len(notes)
    if coverage_weak:
        for note in unmatched:
            content = str(note.get("content", "")).strip()
            bbox = note.get("bbox")
            confidence = note.get("confidence", 0)
            if not content or not isinstance(bbox, list) or len(bbox) != 4 or confidence < 0.5:
                continue
            if any(_box_iou(bbox, kept["bbox"]) >= 0.75 for kept in reconciled):
                continue
            reconciled.append(note)
    return sorted(reconciled, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def _extract_sticky_notes_local(path, timeout, conversion_meta):
    from PIL import Image  # type: ignore

    deadline = time.monotonic() + timeout
    workdir = tempfile.mkdtemp(prefix="oc-note-ocr-")
    try:
        regions = None
        detection_meta = {}
        color_regions, color_meta = _note_regions(path, workdir)
        edge_regions = _edge_regions(path)
        if sys.platform == "darwin":
            detections, apple_meta = _run_apple_vision_rectangles(path, max(0.1, deadline - time.monotonic()))
            if detections is not None:
                vision_regions = [
                    {
                        "box": item.get("box"),
                        "position": item.get("position"),
                        "color": item.get("color", "neutral"),
                        "colorfulness": item.get("colorfulness", 0),
                        "area_fraction": (item.get("geometry") or {}).get("areaFraction", 0),
                        "fill": None,
                        "rectangle_confidence": (item.get("geometry") or {}).get("rectangleConfidence", 0),
                        "text": item.get("text", ""),
                        "confidence": _normalize_ocr_confidence(item.get("confidence", 0), "apple"),
                        "ocr_tool": item.get("ocrTool", "apple-vision"),
                    }
                    for item in detections
                    if isinstance(item, dict) and isinstance(item.get("box"), list) and len(item["box"]) == 4
                ]
                regions = vision_regions + color_regions + edge_regions
                detection_meta = {"engine": "ensemble", **apple_meta, "candidate_count": len(regions), "proposals": {"vision": len(vision_regions), "color": len(color_regions), "edge": len(edge_regions)}}
        if regions is None:
            regions = color_regions + edge_regions
            detection_meta = {"engine": "color-edge", "candidate_count": len(regions), "proposals": {"color": len(color_regions), "edge": len(edge_regions)}}
        normalized = []
        for region in regions:
            if not isinstance(region.get("box"), list) or len(region["box"]) != 4:
                continue
            item = dict(region)
            item["candidate_score"] = _candidate_score(item)
            normalized.append(item)
        regions = _nms_regions(normalized)
        notes = []
        for index, region in enumerate(regions, 1):
            if time.monotonic() >= deadline:
                break
            if region.get("text", "").strip():
                notes.append(region)
                continue
            crop = Image.open(path).convert("RGB").crop(tuple(region["box"]))
            if crop.width * crop.height > NOTE_MAX_CROP_PIXELS:
                factor = (NOTE_MAX_CROP_PIXELS / (crop.width * crop.height)) ** 0.5
                crop = crop.resize((max(1, round(crop.width * factor)), max(1, round(crop.height * factor))), Image.Resampling.LANCZOS)
            crop_path = os.path.join(workdir, f"note-{index}.png")
            crop.save(crop_path, format="PNG")
            text = ""
            tool = None
            confidence = 0.0
            if which("tesseract"):
                text, confidence = _ocr_candidate(crop_path, max(0.1, deadline - time.monotonic()), psm=6)
                tool = "tesseract"
            if (not text.strip()) and sys.platform == "darwin":
                text, apple_meta = _run_apple_vision_ocr(crop_path, max(0.1, deadline - time.monotonic()))
                tool = "apple-vision" if text else None
                confidence = (apple_meta or {}).get("confidence", 0.0)
            if text and text.strip():
                notes.append({**region, "text": text.strip(), "ocr_tool": tool, "confidence": round(confidence, 2)})
        text = "\n\n".join(
            f"Note {index} [{note['color']}] at x={note['position']['x']}, y={note['position']['y']}, "
            f"w={note['position']['width']}, h={note['position']['height']}:\n{note['text']}"
            for index, note in enumerate(notes, 1)
        )
        return text, "sticky-note-crop-ocr", {
            "notes": notes, "detected_regions": len(regions), "recognized_notes": len(notes),
            "detection": detection_meta, "conversion": conversion_meta,
            "candidate_scoring": "color+rectangle+ocr+edge bounded NMS",
            "caps": {"max_regions": NOTE_MAX_REGIONS, "max_crop_pixels": NOTE_MAX_CROP_PIXELS},
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _structured_local_sticky_notes(path, timeout, conversion_meta):
    text, tool, meta = _extract_sticky_notes_local(path, timeout, conversion_meta)
    detection = meta.get("detection", {})
    width, height = (detection.get("size") or detection.get("image_size") or meta.get("size") or [1, 1])
    structured = []
    for index, note in enumerate(meta.get("notes", []), 1):
        box = note.get("box", [])
        if len(box) != 4:
            continue
        content = str(note.get("text", "")).strip()
        if not content:
            continue
        structured.append({
            "id": f"note-{index}",
            "bbox": [round(box[0] / width, 6), round(box[1] / height, 6),
                     round(box[2] / width, 6), round(box[3] / height, 6)],
            "color": str(note.get("color", "unknown")),
            "content": content,
            "confidence": round(min(max(float(note.get("confidence", 0)) / 100, 0), 1), 6),
            "languages": (["zh-Hant"] if any("\u3400" <= char <= "\u9fff" for char in content) else []) + (["en"] if any(char.isascii() and char.isalpha() for char in content) else []),
        })
    payload = {"notes": structured}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), "sticky-note-crop-ocr", {
        **meta, "structured": True, "fallback": "local-ocr", "model": None,
    }


def _extract_sticky_notes(path, timeout, conversion_meta):
    route = _sticky_route()
    if route == "multicrop":
        multicrop = _extract_sticky_notes_multicrop(path, timeout, conversion_meta)
        if multicrop is not None and len(multicrop) == 3:
            return multicrop
        failure_meta = multicrop[1] if multicrop is not None else {
            "request_count": 0, "failure_class": "error", "failure_code": "MULTICROP_ERROR",
            "failure_reason": "multicrop unavailable"
        }
        text, tool, meta = _structured_local_sticky_notes(path, max(0.1, timeout), conversion_meta)
        return text, tool, {**meta, "route": "multicrop-fallback", "fallback": "local-ocr",
                            "request_count": failure_meta["request_count"],
                            "failure_class": failure_meta.get("failure_class", "error"),
                            "failure_code": failure_meta.get("failure_code", "MULTICROP_ERROR"),
                            "failure_reason": failure_meta.get("failure_reason", "unknown failure"),
                            **({"response_shape": failure_meta["response_shape"]} if "response_shape" in failure_meta else {}),
                            **({"failure_predicates": failure_meta["failure_predicates"]} if "failure_predicates" in failure_meta else {}),
                            **({"geometry_evidence": failure_meta["geometry_evidence"]} if "geometry_evidence" in failure_meta else {}),
                            "multicrop": failure_meta}
    if route == "resident-indexed":
        resident = _extract_sticky_notes_resident(path, timeout, conversion_meta)
        if resident is not None:
            return resident
        text, tool, meta = _structured_local_sticky_notes(path, max(0.1, timeout), conversion_meta)
        return text, tool, {**meta, "route": "resident-indexed-fallback", "state_machine": STICKY_RESIDENT_VERSION, "fallback": "local-ocr", "request_count": 0}
    deadline = time.monotonic() + timeout
    from PIL import Image  # type: ignore
    with Image.open(path) as image:
        image_size = image.size
    physical_regions = _physical_note_regions(path, max(0.1, deadline - time.monotonic()))
    primary, primary_meta = _run_sticky_fragment_vlm(
        path, OCR_VLM_MODEL, max(0.1, deadline - time.monotonic())
    )
    if primary is not None:
        fragments = primary["fragments"]
        refined_regions = _refine_fragment_regions(path, fragments, physical_regions, image_size)
        assigned = _assign_fragments_to_regions(fragments, refined_regions, image_size)
        notes = []
        for index, region in enumerate(refined_regions, 1):
            grouped = assigned.get(index, [])
            if not grouped:
                continue
            contents = []
            languages = []
            for fragment in grouped:
                content = fragment["content"].strip()
                if content and content not in contents:
                    contents.append(content)
                for language in fragment["languages"]:
                    if language not in languages:
                        languages.append(language)
            if not contents:
                continue
            box = region["box"]
            notes.append({
                "id": f"note-{len(notes) + 1}",
                "bbox": [round(box[0] / image_size[0], 6), round(box[1] / image_size[1], 6),
                         round(box[2] / image_size[0], 6), round(box[3] / image_size[1], 6)],
                "color": grouped[0]["color"], "content": "\n".join(contents),
                "confidence": max(fragment["confidence"] for fragment in grouped), "languages": languages,
            })
        validated = _validate_sticky_notes({"notes": notes})
        if validated is not None:
            return json.dumps(validated, ensure_ascii=False, separators=(",", ":")), "qwen2.5vl-sticky-fragment-seeds", {
                "route": "legacy",
                "structured": True, "model": OCR_VLM_MODEL, "request": primary_meta,
                "request_count": 1, "note_count": len(notes), "fragment_count": len(fragments),
                "physical_regions": len(refined_regions), "geometry_authority": "local-physical-refinement",
                "fragment_seed_version": STICKY_FRAGMENT_VERSION, "conversion": conversion_meta,
            }

    remaining = max(0.1, deadline - time.monotonic())
    text, tool, meta = _structured_local_sticky_notes(path, remaining, conversion_meta)
    meta.update({"primary": primary_meta, "request_count": 1 if primary_meta.get("request") else 0,
                 "physical_regions": len(physical_regions), "geometry_authority": "local-physical-refinement",
                 "fragment_seed_version": STICKY_FRAGMENT_VERSION})
    return text, tool, meta


def _ocr_quality(text, confidence=0.0):
    value = (text or "").strip()
    if not value:
        return -1.0
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in value) / len(value)
    words = [word for word in value.split() if word]
    alpha_num = sum(ch.isalnum() for ch in value) / len(value)
    lines = [line for line in value.splitlines() if line.strip()]
    return (min(len(value), 4000) / 4000) * 0.25 + printable * 0.25 + alpha_num * 0.2 + min(len(words), 120) / 120 * 0.15 + min(len(lines), 30) / 30 * 0.05 + min(max(confidence, 0), 100) / 100 * 0.1


def extract_image(path, timeout, engine="production"):
    deadline = time.monotonic() + timeout
    workdir = tempfile.mkdtemp(prefix="oc-image-")
    try:
        image_path, conversion_meta = _prepare_image_input(path, workdir, max(0.1, deadline - time.monotonic()))
        if engine == "sticky-notes":
            return _extract_sticky_notes(image_path, max(0.1, deadline - time.monotonic()), conversion_meta)
        document_image, document_meta = _document_image(image_path, max(0.1, deadline - time.monotonic()))
        if engine == "document" and not document_image:
            return None, None, {"why": "not a detected document image", "document_image": False, "document_features": document_meta}
        try:
            primary, primary_meta = _image_derivative(image_path, workdir, 0)
        except Exception as error:
            return None, None, {"why": f"image preprocessing failed: {error}"}

        if engine == "apple":
            text, meta = _run_apple_vision_ocr(primary, max(0.1, deadline - time.monotonic()))
            return (text, "apple-vision", {"apple_vision": meta, "preprocessing": primary_meta, "conversion": conversion_meta, "document_image": document_image}) if text else (None, None, {**(meta or {}), "conversion": conversion_meta})

        if engine == "tesseract":
            if not which("tesseract"):
                return None, None, {"why": "tesseract not installed"}
            rc, text, err = _run_tesseract(primary, max(0.1, deadline - time.monotonic()), psm=11)
            return (text.strip(), "tesseract", {"preprocessing": primary_meta, "conversion": conversion_meta, "psm": 11, "document_image": document_image}) if rc == 0 and text.strip() else (None, None, {"why": err[-400:], "conversion": conversion_meta, "document_image": document_image})

        if not which("tesseract"):
            text, meta = _run_apple_vision_ocr(primary, max(0.1, deadline - time.monotonic()))
            if text and text.strip():
                return text, "apple-vision", {**(meta or {}), "preprocessing": primary_meta, "conversion": conversion_meta, "document_image": document_image}
            return None, None, {"why": "tesseract not installed", "apple_vision": meta, "conversion": conversion_meta, "document_image": document_image}

        # Rotation selection is bounded to four upright hypotheses. OSD is only
        # a hint and can confidently choose the wrong direction on sparse scans.
        best = ("", -1.0, primary_meta, 11, 0, primary)
        for angle in (0, 90, 180, 270):
            if time.monotonic() >= deadline:
                break
            candidate = primary
            candidate_meta = primary_meta
            if angle:
                candidate, candidate_meta = _image_derivative(image_path, workdir, angle)
            text_candidate, score = _ocr_candidate(candidate, max(0.1, deadline - time.monotonic()), psm=11)
            if score > best[1] or (score == best[1] and len(text_candidate) > len(best[0])):
                best = (text_candidate, score, candidate_meta, 11, angle, candidate)

        # Sparse/layout-heavy images can score poorly with PSM 11. Try PSM 3,
        # then PSM 6, retaining confidence-first ordering and bounded work.
        if best[1] < 45 and time.monotonic() < deadline:
            for psm in (3, 6):
                text_candidate, score = _ocr_candidate(primary, max(0.1, deadline - time.monotonic()), psm=psm)
                if score > best[1] or (score == best[1] and len(text_candidate) > len(best[0])):
                    best = (text_candidate, score, primary_meta, psm, 0, primary)

        tesseract_text = best[0].strip() if best[0].strip() and best[1] >= 45 else ""
        if tesseract_text and os.path.exists(best[5]):
            rc, direct_text, _ = _run_tesseract(best[5], max(0.1, deadline - time.monotonic()), psm=best[3])
            if rc == 0 and direct_text.strip():
                tesseract_text = direct_text.strip()
        apple_text, apple_meta = _run_apple_vision_ocr(best[5], max(0.1, deadline - time.monotonic()))
        candidates = [(tesseract_text, "tesseract", {"confidence": best[1], "psm": best[3], "rotation": best[4]}), (apple_text or "", "apple-vision", {"confidence": (apple_meta or {}).get("confidence", 0), "apple_vision": apple_meta})]
        candidates = [(text, tool, meta) for text, tool, meta in candidates if text.strip()]
        if candidates:
            selected = max(candidates, key=lambda item: _ocr_quality(item[0], item[2].get("confidence", 0)))
            return selected[0].strip(), selected[1], {"preprocessing": best[2], "conversion": conversion_meta, "selection": "generic-quality-policy", "quality": _ocr_quality(selected[0], selected[2].get("confidence", 0)), "candidates": [{"tool": tool, "quality": _ocr_quality(text, meta.get("confidence", 0))} for text, tool, meta in candidates], "document_image": document_image, "document_features": document_meta, **selected[2]}

        rc, out, err = _run_tesseract(primary, max(0.1, deadline - time.monotonic()))
        if rc == 0 and out.strip():
            return out, "tesseract", {"preprocessing": primary_meta, "conversion": conversion_meta, "document_image": document_image, "document_features": document_meta}

        ext = ".png"
        retry_dir = tempfile.mkdtemp(prefix="oc-ocr-")
        try:
            workfile = os.path.join(retry_dir, "img" + ext)
            shutil.copyfile(primary, workfile)
            rc2, out2, err2 = _run_tesseract(workfile, max(0.1, deadline - time.monotonic()))
            if rc2 == 0 and out2.strip():
                return out2, "tesseract", {"retried_via": "tmpdir-copy", "preprocessing": primary_meta, "conversion": conversion_meta}
            why = (err2 or err or "tesseract produced no text")[-400:]
            return None, None, {"why": why, "conversion": conversion_meta, "document_image": document_image, "document_features": document_meta}
        finally:
            shutil.rmtree(retry_dir, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- zip archive ----------
def _safe_extract_target(dest_root, name):
    """Resolve a zip member name under dest_root, rejecting zip-slip / absolute paths."""
    target = os.path.realpath(os.path.join(dest_root, name))
    base = os.path.realpath(dest_root)
    if target == base:
        return None
    if not target.startswith(base + os.sep):
        return None
    return target


def extract_zip(path):
    if not zipfile.is_zipfile(path):
        return None, None, {"why": "not a valid zip archive"}
    key = cache_key(path, "archive", "unzip", 0)
    dest = os.path.join(CACHE_DIR, "unzip", key)
    files = []
    total = 0
    truncated = False
    skipped = []
    try:
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            for info in infos:
                if len(files) >= ZIP_MAX_FILES:
                    truncated = True
                    break
                target = _safe_extract_target(dest, info.filename)
                if target is None:
                    skipped.append(info.filename)
                    continue
                if total + info.file_size > ZIP_MAX_TOTAL_BYTES:
                    truncated = True
                    break
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if not (os.path.exists(target) and os.path.getsize(target) == info.file_size):
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 64)
                total += info.file_size
                kind = classify(target, "", "auto")
                guessed_mime, _ = mimetypes.guess_type(target)
                files.append({
                    "path": target,
                    "name": info.filename,
                    "kind": kind,
                    "mime": guessed_mime or "",
                    "size": info.file_size,
                })
    except Exception as e:
        return None, None, {"why": f"zip extraction failed: {e}"}
    lines = [f"Archive contained {len(files)} extractable file(s), unpacked to {dest}:"]
    for f in files:
        lines.append(f"  - {f['name']} [{f['kind']}, {f['size']} bytes]")
    if skipped:
        lines.append(f"Skipped {len(skipped)} unsafe entr(y/ies) (path traversal): " + ", ".join(skipped[:10]))
    if truncated:
        lines.append(f"NOTE: extraction truncated at {ZIP_MAX_FILES} files / {ZIP_MAX_TOTAL_BYTES} bytes cap.")
    text = "\n".join(lines)
    meta = {
        "files": files,
        "file_count": len(files),
        "dest": dest,
        "truncated": truncated,
        "skipped": skipped,
    }
    return text, "zipfile", meta


# ---------- plain text ----------
def extract_text(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(), "plain", {}
    except Exception as e:
        return None, None, {"why": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--mime", default="")
    ap.add_argument("--kind", default="auto")
    ap.add_argument("--max-chars", type=int, default=60000)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--model", default="base")
    ap.add_argument("--no-cache", action="store_true", help="Disable cache reads and writes")
    ap.add_argument("--cache-bypass-read", action="store_true", help="Skip cache read but write successful result")
    ap.add_argument("--ocr-correction", action="store_true", help="Enable local optional OCR correction")
    ap.add_argument("--ocr-mode", choices=["baseline", "correction"], default=None)
    ap.add_argument("--ocr-engine", choices=["production", "apple", "tesseract", "document", "sticky-notes"], default="production")
    ap.add_argument("--classify-only", action="store_true")
    ap.add_argument("--audit-only", action="store_true")
    args = ap.parse_args()

    if args.ocr_mode == "correction":
        args.ocr_correction = True
    elif args.ocr_mode == "baseline":
        args.ocr_correction = False

    path = args.file
    result = {
        "status": "error",
        "kind": "unknown",
        "text": "",
        "truncated": False,
        "chars": 0,
        "tool": None,
        "cached": False,
        "meta": {},
        "provenance": {
            "pipeline": "ocr-first",
            "ocr_mode": "correction" if args.ocr_correction else "baseline",
            "authoritative_text": "text/original_text",
            "candidate_enabled": False,
            "production_enabled": os.environ.get("OCR_PRODUCTION_ENABLED", "0").lower() in {"1", "true", "yes"},
        },
    }

    if not os.path.exists(path):
        result["status"] = "not_found"
        result["detail"] = f"file not found: {path}"
        print(json.dumps(result)); return

    kind = classify(path, args.mime, args.kind)
    result["kind"] = kind
    guessed_mime, _ = mimetypes.guess_type(path)
    result["provenance"]["audit"] = _extractor_audit(path, args.mime or guessed_mime or "", args.timeout)

    if args.audit_only:
        result.update({"status": "ok", "text": "", "tool": "audit", "provenance": {**result["provenance"], "audit_only": True}})
        print(json.dumps(result)); return

    if kind == "unknown":
        result["status"] = "unavailable"
        result["detail"] = "could not determine media kind from mime/extension"
        print(json.dumps(result)); return

    if args.classify_only:
        if kind == "image":
            classify_workdir = tempfile.mkdtemp(prefix="oc-image-classify-")
            try:
                image_path, conversion = _prepare_image_input(path, classify_workdir, args.timeout)
                if args.ocr_engine == "sticky-notes":
                    regions, features = _note_regions(image_path, classify_workdir)
                    result.update({"status": "ok", "text": "", "meta": {"sticky_notes": bool(regions), "note_regions": regions, "note_features": features, "conversion": conversion}, "provenance": {**result["provenance"], "classification_only": True}})
                else:
                    classification, classification_meta = _classify_image(image_path, args.timeout)
                    result.update({"status": "ok", "text": "", "meta": {"classification": classification, "document_image": classification["label"] in {"document", "handwriting"}, "document_features": classification.get("features", {}), "conversion": conversion, **classification_meta}, "provenance": {**result["provenance"], "classification_only": True}})
            except subprocess.TimeoutExpired:
                result["status"] = "timeout"
                result["detail"] = f"extraction exceeded {args.timeout}s"
            except Exception as error:
                result["status"] = "unavailable"
                result["detail"] = str(error)
            finally:
                shutil.rmtree(classify_workdir, ignore_errors=True)
        else:
            result.update({"status": "ok", "text": "", "meta": {"document_image": False}, "provenance": {**result["provenance"], "classification_only": True}})
        print(json.dumps(result)); return

    key = cache_key(path, kind, args.model, args.max_chars, args.ocr_correction, args.ocr_engine)
    if not args.no_cache and not args.cache_bypass_read and not args.classify_only:
        cached = cache_get(key)
        if isinstance(cached, dict):
            cached["cached"] = True
            cached.setdefault("provenance", {}).setdefault("audit", result["provenance"]["audit"])
            print(json.dumps(cached)); return

    try:
        if kind == "pdf":
            text, tool, meta = extract_pdf(path, args.timeout)
        elif kind == "audio":
            skill_model = "turbo" if args.model == "base" else args.model
            try:
                text, tool, meta = transcribe_via_skill(path, args.timeout, model=skill_model)
            except subprocess.TimeoutExpired:
                raise
            except Exception as e:
                text, tool, meta = None, None, {"why": f"transcribe_via_skill raised exception: {str(e)}"}
            if not text:
                text, tool, whisper_meta = extract_whisper(path, args.model, args.timeout)
                if meta:
                    whisper_meta.update(meta)
                meta = whisper_meta
        elif kind == "video":
            try:
                text, tool, meta = extract_video(path, args.model, args.timeout)
            except subprocess.TimeoutExpired:
                raise
            except Exception as e:
                text, tool, meta = None, None, {"why": f"extract_video raised exception: {str(e)}"}
            if not text and (not meta or not meta.get("frames")):
                text, tool, whisper_meta = extract_whisper(path, args.model, args.timeout)
                if meta:
                    whisper_meta.update(meta)
                meta = whisper_meta
        elif kind == "image":
            text, tool, meta = extract_image(path, args.timeout, args.ocr_engine)
        elif kind == "archive":
            try:
                text, tool, meta = extract_zip(path)
            except Exception as e:
                text, tool, meta = None, None, {"why": f"extract_zip raised exception: {str(e)}"}
        elif kind == "text":
            text, tool, meta = extract_text(path)
        else:
            text, tool, meta = None, None, {}
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["detail"] = f"extraction exceeded {args.timeout}s"
        print(json.dumps(result)); return
    except Exception as e:
        result["status"] = "error"
        result["detail"] = str(e)
        print(json.dumps(result)); return

    has_frames = bool(meta and meta.get("frames"))
    if (not text or not text.strip()) and not has_frames:
        result["status"] = "unavailable"
        result["meta"] = meta or {}
        result["detail"] = (meta or {}).get("why", "no extractable text / backend unavailable")
        print(json.dumps(result)); return

    text = (text or "").replace("\x00", "").strip()
    original_text = text
    correction_meta = {"status": "not_applicable"}
    if args.ocr_correction and kind in {"image", "pdf"} and text:
        previous = os.environ.get("OCR_CORRECTION_ENABLED")
        os.environ["OCR_CORRECTION_ENABLED"] = "1"
        try:
            text, correction_meta = correct_ocr_text(text, max(0.1, args.timeout))
        finally:
            if previous is None:
                os.environ.pop("OCR_CORRECTION_ENABLED", None)
            else:
                os.environ["OCR_CORRECTION_ENABLED"] = previous
        meta = dict(meta or {})
        meta["ocr_correction"] = correction_meta
    full_len = len(original_text)
    corrected_full_len = len(text)
    bounded_fields, inline_bounds = bounded_correction_fields(original_text, text, args.max_chars)
    bounded_original = bounded_fields["original_text"]
    bounded_corrected = bounded_fields["corrected_text"] or ""
    truncated = inline_bounds["original_truncated"] or inline_bounds["corrected_truncated"]

    result.update({
        "status": "ok",
        "truncated": truncated,
        "chars": len(bounded_original),
        "full_chars": full_len,
        "corrected_full_chars": corrected_full_len,
        "tool": tool,
        "meta": meta or {},
    })
    result.update(bounded_fields)
    result["inline_chars"] = len(bounded_original) + len(bounded_corrected)
    result["provenance"].update({
        "ocr_tool": tool,
        "correction": correction_meta,
        "corrected_text_available": bool(result.get("corrected_text")),
    })
    if not args.no_cache:
        cache_put(key, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
