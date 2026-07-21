#!/usr/bin/env bash
# setup.sh — non-fatal doctor for the media-guard extractor (extract.py).
# Reports which backends are available and how to install the missing ones.
# Exit code is always 0 so it can be sourced from installers without breaking them.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="$(command -v python3 || true)"
echo "media-guard extractor — backend check"
echo "python3: ${py:-MISSING}"

check_cli() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  [ok]   $1 ($(command -v "$1"))"
  else
    echo "  [MISS] $1 — $2"
  fi
}
check_py() {
  if [ -n "$py" ] && "$py" -c "import $1" >/dev/null 2>&1; then
    echo "  [ok]   python:$1"
  else
    echo "  [MISS] python:$1 — $2"
  fi
}

echo "PDF backends (need at least one):"
check_cli pdftotext "brew install poppler"
check_py  fitz       "pip install pymupdf   (or: brew install pymupdf)"
check_py  pypdf      "pip install pypdf"

echo "Audio/Video backend:"
check_cli whisper "pip install -U openai-whisper"
check_cli ffmpeg  "brew install ffmpeg   (whisper needs it to decode media)"

echo "Image OCR backend:"
check_cli tesseract "brew install tesseract"

echo "VLM OCR backend (preferred image + scanned-PDF OCR):"
ocr_vlm_base_url="${OCR_VLM_BASE_URL:-http://127.0.0.1:11434}"
ocr_vlm_model="${OCR_VLM_MODEL:-qwen3-vl:32b}"
ollama_tags=""
if command -v curl >/dev/null 2>&1 && ollama_tags="$(curl -fsS --connect-timeout 2 --max-time 5 "${ocr_vlm_base_url%/}/api/tags" 2>/dev/null)"; then
  echo "  [ok]   Ollama reachable at ${ocr_vlm_base_url}"
  if printf '%s' "$ollama_tags" | grep -Eq '"name"[[:space:]]*:[[:space:]]*"'"$ocr_vlm_model"'"'; then
    echo "  [ok]   Ollama model ${ocr_vlm_model}"
  else
    echo "  [MISS] Ollama model ${ocr_vlm_model} — ollama pull qwen3-vl:32b"
  fi
else
  echo "  [MISS] Ollama at ${ocr_vlm_base_url} — start ollama (brew services start ollama)"
fi

fitz_py="$SCRIPT_DIR/../../../claude-code/skills/tool--pdf/scripts/.venv/bin/python"
if [ -x "$fitz_py" ] && "$fitz_py" -c 'import fitz' >/dev/null 2>&1; then
  echo "  [ok]   fitz rasterizer (${fitz_py})"
else
  echo "  [MISS] fitz rasterizer (${fitz_py}) — cd config/claude-code/skills/tool--pdf/scripts && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

echo
echo "Notes:"
echo " - Missing backends degrade gracefully: the affected media type just gets a"
echo "   pointer note routing to the relevant skill instead of inline text."
echo " - VLM OCR degrades to tesseract/PyMuPDF when Ollama or its model is unavailable;"
echo "   OCR_VLM_ENABLED=0 forces the classic path."
echo " - First whisper run downloads the model (~140MB for 'base')."
exit 0
