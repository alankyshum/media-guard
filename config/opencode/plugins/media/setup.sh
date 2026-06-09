#!/usr/bin/env bash
# setup.sh — non-fatal doctor for the media-guard extractor (extract.py).
# Reports which backends are available and how to install the missing ones.
# Exit code is always 0 so it can be sourced from installers without breaking them.
set -u

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

echo
echo "Notes:"
echo " - Missing backends degrade gracefully: the affected media type just gets a"
echo "   pointer note routing to the relevant skill instead of inline text."
echo " - First whisper run downloads the model (~140MB for 'base')."
exit 0
