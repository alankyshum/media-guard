# media-guard

Stops attached **PDF / audio / video / image** files from crashing an opencode
session on providers that reject those media types, and instead inlines a
**locally-extracted text** version so the model still gets the content.

Without this, attaching e.g. a PDF makes opencode ship raw bytes to the provider
and the whole request fails with:

```
'file part media type application/pdf' functionality not supported
```

## Pieces

| File            | Role                                                                    |
| --------------- | ----------------------------------------------------------------------- |
| `../media-guard.ts` | Plugin. Hooks `experimental.chat.messages.transform`; swaps unsupported file parts for synthetic text parts. Registers a `media_extract` tool for on-demand parsing. |
| `extract.py`    | Deterministic CLI dispatcher. `file -> {status, kind, text, ...}` JSON.  |
| `setup.sh`      | Non-fatal backend doctor + install hints.                                |

## extract.py

```
python3 extract.py <file> [--mime MIME] [--kind auto|pdf|audio|video|image|text]
        [--max-chars 60000] [--timeout 180] [--model base] [--no-cache]
```

Prints one JSON object, always exits 0. `status` is one of
`ok | unavailable | too_large | timeout | error | not_found`.

Backends (all optional, degrade gracefully):

- **pdf** → `pdftotext` → PyMuPDF → `pypdf` → [if no text] rasterize + VLM OCR → PyMuPDF `get_textpage_ocr`
- **audio/video** → `whisper` CLI (ffmpeg-decoded), default model `base`
- **image** → Ollama VLM (`qwen3-vl:32b`) → `tesseract`
- **text** → read directly

## VLM OCR

Image OCR uses the local Ollama VLM first. Scanned-PDF OCR only runs when direct
text extraction produces no text: pages are rasterized by the PyMuPDF-capable
interpreter in the `tool--pdf` skill venv, then sent to the VLM. If Ollama or the
model is unavailable, extraction falls back to tesseract/PyMuPDF OCR. Set
`OCR_VLM_ENABLED=0` to force the classic path.

Optional environment variables:

- `OCR_VLM_ENABLED` — default `1`; set `0` to disable VLM OCR.
- `OCR_VLM_BASE_URL` — default `http://127.0.0.1:11434`.
- `OCR_VLM_MODEL` — default `qwen3-vl:32b`.
- `OCR_VLM_NUM_CTX` — default `16384`.
- `OCR_PDF_MAX_PAGES` — default `20`; maximum scanned-PDF pages OCR'd.

Expensive results (transcription, OCR) are cached on disk under
`$TMPDIR/opencode-media-cache`, keyed by
`sha256(path,size,mtime,kind,model,max_chars,vlm-enabled,vlm-model,pdf-max-pages)`,
so switching VLM state, model, or scanned-PDF page cap invalidates stale OCR.
Repeated turns and restarts are cheap. Use `--no-cache` to bypass.

## Relationship to skills

This replaces the *extraction* path of the `tool--transcribe` skill and the
*read-text/OCR* path of the `tool--pdf` skill — those now happen automatically
on attach. The skills are still useful for the richer, non-extraction work they
do (PDF form-filling / generation, transcript analysis), so they are kept but
slimmed. See each skill's SKILL.md note.

## Install missing backends

```bash
bash ~/.config/opencode/plugins/media/setup.sh   # report
brew install poppler ffmpeg tesseract            # common installs
pip install -U openai-whisper pymupdf pypdf
ollama pull qwen3-vl:32b
```
