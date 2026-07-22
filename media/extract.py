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
import zipfile

CACHE_DIR = os.environ.get("MEDIA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "opencode-media-cache"))
CACHE_VERSION = "v3-image-orientation"
IMAGE_PREPROCESS_VERSION = "v2-deterministic-rotation"
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
NOTE_MIN_COLORFULNESS = float(os.environ.get("MEDIA_NOTE_MIN_COLORFULNESS", "0.30"))
NOTE_MIN_CANDIDATE_SCORE = float(os.environ.get("MEDIA_NOTE_MIN_CANDIDATE_SCORE", "0.35"))
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
OCR_VLM_MODEL = os.environ.get("OCR_VLM_MODEL", "qwen3-vl:32b")
OCR_CORRECTION_MODEL = os.environ.get("OCR_CORRECTION_MODEL", "qwen2.5vl:7b")


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


APPLE_VISION_OCR_BIN = os.environ.get("APPLE_VISION_OCR_BIN", _apple_vision_cache_path())
OCR_VLM_NUM_CTX = int(os.environ.get("OCR_VLM_NUM_CTX", "16384"))
try:
    OCR_PDF_MAX_PAGES = int(os.environ.get("OCR_PDF_MAX_PAGES", "20"))
except (TypeError, ValueError):
    OCR_PDF_MAX_PAGES = 20


def _vlm_enabled():
    return os.environ.get("OCR_VLM_ENABLED", "1").strip().lower() not in {"0", "false"}


def _correction_enabled():
    return os.environ.get("OCR_CORRECTION_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


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
    ocr_sig = f"{IMAGE_PREPROCESS_VERSION}:{_vlm_enabled()}:{OCR_VLM_MODEL}:{OCR_PDF_MAX_PAGES}:{correction_enabled}:{OCR_CORRECTION_MODEL}"
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
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        if isinstance(e, urllib.error.HTTPError):
            reason = f"HTTP {e.code}: {e.reason}"
        return None, None, {"why": reason[-400:]}


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
    scale = min(1.0, 256 / max(width, height))
    small = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    sw, sh = small.size
    pixels = list(small.getdata())
    mask = bytearray(sw * sh)
    for index, (r, g, b) in enumerate(pixels):
        value = (r + g + b) / 3
        chroma = (max(r, g, b) - min(r, g, b)) / 255
        mask[index] = 1 if value >= 125 and chroma >= 0.24 else 0

    # Close text/shadow holes before connected-component extraction. Sampling is
    # bounded, so this cannot expand work with source resolution.
    mask_image = Image.frombytes("L", (sw, sh), bytes(255 if value else 0 for value in mask))
    mask_image = mask_image.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(9))
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
    regions.sort(key=lambda item: (item["box"][1], item["box"][0]))
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


def _candidate_score(candidate):
    geometry = candidate.get("geometry") or {}
    colorfulness = min(max(float(candidate.get("colorfulness") or 0), 0), 1)
    rectangle = min(max(float(candidate.get("rectangle_confidence") or geometry.get("rectangleConfidence", 0)), 0), 1)
    text_confidence = min(max(float(candidate.get("confidence") or 0), 0), 100) / 100
    edge_density = min(max(float(candidate.get("edge_density") or 0), 0), 1)
    area = float(candidate.get("area_fraction") or geometry.get("areaFraction", 0))
    size_score = 1.0 if 0.01 <= area <= 0.60 else 0.0
    return round(0.35 * colorfulness + 0.25 * rectangle + 0.20 * text_confidence + 0.10 * min(edge_density * 4, 1) + 0.10 * size_score, 6)


def _nms_regions(candidates, threshold=0.55):
    ranked = sorted(
        (item for item in candidates if item["candidate_score"] >= NOTE_MIN_CANDIDATE_SCORE),
        key=lambda item: (item["candidate_score"], item.get("confidence", 0), item["box"][1], item["box"][0]),
        reverse=True,
    )
    selected = []
    for candidate in ranked:
        if any(_box_iou(candidate["box"], previous["box"]) >= threshold or _overlap_smaller(candidate["box"], previous["box"]) >= threshold for previous in selected):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item["box"][1], item["box"][0]))[:NOTE_MAX_REGIONS]


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


def _extract_sticky_notes(path, timeout, conversion_meta):
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
            if region.get("ocr_tool") == "apple-vision" and not region.get("text", "").strip():
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
                    document_image, features = _document_image(image_path, args.timeout)
                    result.update({"status": "ok", "text": "", "meta": {"document_image": document_image, "document_features": features, "conversion": conversion}, "provenance": {**result["provenance"], "classification_only": True}})
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
