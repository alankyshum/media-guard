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
- **image** → document classifier + bounded Apple Vision/Tesseract OCR selection; macOS HEIC/HEIF is converted to a disposable PNG with system `sips` when Pillow cannot decode it; sticky-note images use saturated-region crop OCR; other images use local vision digest
- **text** → read directly

## VLM OCR

Image OCR uses disposable grayscale derivatives; the source image is never
overwritten. Rotation is selected from bounded 0/90/180/270 hypotheses by mean
word confidence, not raw OSD direction. Upright grayscale PSM 11 is primary;
PSM 3 then PSM 6 probe the same upright image only when confidence is low.
Ties prefer longer extracted text. Images below the minimum size are upscaled
only for preprocessing. Timeouts, corrupt images, missing Pillow, Tesseract, or
VLM fall back to the next local path or return `unavailable`.

On macOS, `setup.sh` builds the optional `apple-vision-ocr.swift` bridge when
`swiftc` is available. Document images run Apple Vision and Tesseract through
the same generic quality policy; selected output records engine and scores.
Non-document images remain on local vision digest path. No remote OCR fallback
is used.

Sticky-note route detects connected, saturated, paper-like regions generically,
OCRs only bounded crops, returns normalized position/color/text, caps region and
crop size, and removes derivatives. No VLM correction, background OCR, or
fixture-specific coordinates/text is used.

Scanned-PDF OCR only runs when direct
text extraction produces no text: pages are rasterized by the PyMuPDF-capable
interpreter in the `tool--pdf` skill venv, then sent to the VLM. If Ollama or the
model is unavailable, extraction falls back to tesseract/PyMuPDF OCR. Set
`OCR_VLM_ENABLED=0` to force the classic path.

Sticky-note detection is conservative: low-colorfulness, low-fill, thin, and
oversized connected regions are rejected. The output is safe only when the
independent fixture/oracle benchmark passes; otherwise use ordinary local OCR.

Optional environment variables:

- `OCR_VLM_ENABLED` — default `1`; set `0` to disable VLM OCR.
- `OCR_VLM_BASE_URL` — default `http://127.0.0.1:11434`.
- `OCR_VLM_MODEL` — default `qwen3-vl:32b`.
- `OCR_VLM_NUM_CTX` — default `16384`.
- `OCR_PDF_MAX_PAGES` — default `20`; maximum scanned-PDF pages OCR'd.

## Local vision benchmark rollback

The private fixture, oracle, and manifest stay outside the repository. Run the
benchmark against the external manifest; its hash-bound entries reference the
fixture and independently sourced oracle without copying either into source
control:

```bash
bun ~/.config/opencode/external/media-guard/tests/benchmark_local_vision.mjs \
  --manifest /private/path/manifest.json --check
```

Use `--dry-run` before inference and `--out /private/path/run-artifacts` for an
immutable run directory. Never use synthetic self-test output as corpus evidence.
To roll back the selected model, restore `visionModel` in `opencode.jsonc`, then rerun
`bun verify_vision.mjs` and
`bun verify_media_guard_config.mjs` with `MEDIA_GUARD_SELECTED_MODEL` set to that
baseline. Restore the selected values after recording the rollback result.

Candidate vision and OCR correction are disabled by default. Enabling either
requires the matching config flag plus an evidence artifact whose SHA-256 matches
`evidenceSha256`. The artifact must be schema v2, bind the immutable manifest and
JSONL result hashes, prove human holdout provenance, identity, safety, RSS, and
no-remote-fallback gates. A bare `PASS` field or synthetic self-test output is not
authorization. Without that proof, Media Guard uses the deterministic OCR path and
configured baseline vision model.

One-step rollback: set `visionCandidateEnabled` and `ocrCorrection` to `false`,
restore `visionModel` to `gemma4:12b`, clear `evidencePath` and `evidenceSha256`,
then restart opencode. Cached candidate/correction results cannot affect the
baseline path.

OCR responses retain original OCR in `text` and `original_text` as authoritative.
If correction is explicitly enabled and evidence-gated, `corrected_text` exposes
accepted correction as a labeled alternative; `meta.ocr_correction` records
accepted source/replacement spans and provenance. Timeout or abstention leaves the
alternative absent.

Expensive results (transcription, OCR) are cached on disk under
`$TMPDIR/opencode-media-cache`, keyed by
`sha256(path,size,mtime,kind,model,max_chars,image-preprocessing-version,vlm-enabled,vlm-model,pdf-max-pages)`,
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
bash ~/.config/opencode/external/media-guard/media/setup.sh   # report
brew install poppler ffmpeg tesseract            # common installs
pip install -U openai-whisper pymupdf pypdf
ollama pull qwen3-vl:32b
```
