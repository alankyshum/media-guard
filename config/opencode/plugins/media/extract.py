#!/usr/bin/env python3
"""
extract.py — deterministic local media -> text dispatcher for opencode's
media-guard plugin. Consolidates the extraction logic that used to live in the
`tool--pdf` and `tool--transcribe` skills so attachments can be parsed inline
without shipping raw bytes to a remote model.

Usage:
    extract.py <file> [--mime MIME] [--kind KIND] [--max-chars N]
               [--timeout SECONDS] [--model base] [--no-cache]

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
    image  -> tesseract OCR
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
import time
import urllib.error
import urllib.request
import zipfile

CACHE_DIR = os.path.join(tempfile.gettempdir(), "opencode-media-cache")

PDF_MIMES = {"application/pdf"}
AUDIO_PREFIX = "audio/"
VIDEO_PREFIX = "video/"
IMAGE_PREFIX = "image/"

PDF_EXT = {".pdf"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".flv", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
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
OCR_VLM_NUM_CTX = int(os.environ.get("OCR_VLM_NUM_CTX", "16384"))
try:
    OCR_PDF_MAX_PAGES = int(os.environ.get("OCR_PDF_MAX_PAGES", "20"))
except (TypeError, ValueError):
    OCR_PDF_MAX_PAGES = 20


def _vlm_enabled():
    return os.environ.get("OCR_VLM_ENABLED", "1").strip().lower() not in {"0", "false"}


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


def cache_key(path, kind, model, max_chars):
    ocr_sig = f"{_vlm_enabled()}:{OCR_VLM_MODEL}:{OCR_PDF_MAX_PAGES}"
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime_ns)}|{kind}|{model}|{max_chars}|{ocr_sig}"
    except OSError:
        sig = f"{os.path.abspath(path)}|nostat|{kind}|{model}|{max_chars}|{ocr_sig}"
    return hashlib.sha256(sig.encode()).hexdigest()


def cache_get(key):
    p = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(p, "r") as f:
            return json.load(f)
    except Exception:
        return None


def cache_put(key, obj):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, key + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, p)
    except Exception:
        pass


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


def _run_tesseract(img_path, timeout):
    proc = subprocess.run(
        ["tesseract", img_path, "stdout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def extract_image(path, timeout):
    deadline = time.monotonic() + timeout
    text, tool, meta = _run_vlm_ocr(path, max(0.1, deadline - time.monotonic()))
    if text and text.strip():
        return text, tool, meta
    if not which("tesseract"):
        fallback_meta = {"why": "tesseract not installed"}
        if meta:
            fallback_meta["vlm"] = meta
        return None, None, fallback_meta
    # 1) direct
        try:
            rc, out, err = _run_tesseract(path, max(5.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise
        if rc == 0 and out.strip():
            fallback_meta = {}
            if meta:
                fallback_meta["vlm"] = meta
            return out, "tesseract", fallback_meta

    # 2) leptonica on this platform can fail to open certain paths (notably
    #    files under literal /tmp) or odd filenames. Retry against a copy in a
    #    clean $TMPDIR workdir with a simple ASCII name — keeps parsing LOCAL.
    ext = os.path.splitext(path)[1].lower() or ".png"
    workdir = tempfile.mkdtemp(prefix="oc-ocr-")
    try:
        workfile = os.path.join(workdir, "img" + ext)
        try:
            shutil.copyfile(path, workfile)
        except Exception as e:
            return None, None, {"why": f"tesseract failed and copy for retry failed: {e}"}
        try:
            rc2, out2, err2 = _run_tesseract(workfile, max(5.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise
        if rc2 == 0 and out2.strip():
            fallback_meta = {"retried_via": "tmpdir-copy"}
            if meta:
                fallback_meta["vlm"] = meta
            return out2, "tesseract", fallback_meta
        why = (err2 or err or "tesseract produced no text")[-400:]
        fallback_meta = {"why": why}
        if meta:
            fallback_meta["vlm"] = meta
        return None, None, fallback_meta
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
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

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

    key = cache_key(path, kind, args.model, args.max_chars)
    if not args.no_cache:
        cached = cache_get(key)
        if cached is not None:
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
            text, tool, meta = extract_image(path, args.timeout)
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
    full_len = len(text)
    truncated = False
    if full_len > args.max_chars:
        text = text[: args.max_chars]
        truncated = True

    result.update({
        "status": "ok",
        "text": text,
        "truncated": truncated,
        "chars": len(text),
        "full_chars": full_len,
        "tool": tool,
        "meta": meta or {},
    })
    if not args.no_cache:
        cache_put(key, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
