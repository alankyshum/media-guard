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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

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
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT or (m.startswith("text/")):
        return "text"
    return "unknown"


def cache_key(path, kind, model, max_chars):
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime_ns)}|{kind}|{model}|{max_chars}"
    except OSError:
        sig = f"{os.path.abspath(path)}|nostat|{kind}|{model}|{max_chars}"
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
        doc = fitz.open(path)
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
    return None, None, {}


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
def _run_tesseract(img_path, timeout):
    proc = subprocess.run(
        ["tesseract", img_path, "stdout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def extract_image(path, timeout):
    if not which("tesseract"):
        return None, None, {"why": "tesseract not installed"}
    # 1) direct
    try:
        rc, out, err = _run_tesseract(path, timeout)
    except subprocess.TimeoutExpired:
        raise
    if rc == 0 and out.strip():
        return out, "tesseract", {}

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
            rc2, out2, err2 = _run_tesseract(workfile, timeout)
        except subprocess.TimeoutExpired:
            raise
        if rc2 == 0 and out2.strip():
            return out2, "tesseract", {"retried_via": "tmpdir-copy"}
        why = (err2 or err or "tesseract produced no text")[-400:]
        return None, None, {"why": why}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
