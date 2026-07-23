# media-guard

Stops attached **PDF / audio / video / image** files from crashing an opencode
session on providers that reject those media types, and instead inlines a
**locally-extracted text** version so the model still gets the content.

Without this, attaching e.g. a PDF makes opencode ship raw bytes to the provider
and the whole request fails with:

```
'file part media type application/pdf' functionality not supported
```

If candidate vision is requested but its evidence is missing or invalid, the
request is rejected before model selection and every matched media file uses
deterministic local extraction. No local `/api/chat` request is made on that
path. A valid but disabled candidate request intentionally retains configured
baseline local vision behavior.

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
- **image** → local Granite-Docling-first classifier (`document|handwriting|photo|sticky_notes`) when the configured Ollama tag is installed; unavailable/malformed responses fail closed to the deterministic classifier. Sticky-note photos build bounded generic physical-sheet proposals from color, contrast, edge, and rectangle evidence. Sticky routing is exclusive: `OCR_STICKY_MULTICROP_ENABLED=1` selects multicrop before indexed resident routing, even when both flags are set. Optional resident qwen2.5vl:32b mode uses exactly two localhost turns: discovery, then indexed verification. Turn 2 receives every bounded local proposal ID; discovery IDs are telemetry only and never gate verification. Proposal, discovery, verifier, operation, and result diagnostics record request/privacy/geometry state. The model may only select strict IDs/operations; local geometry owns merge/split output. Invalid schema, IDs, caps, deadlines, model errors, and disabled mode fall back to bounded local OCR. Document/handwriting images use bounded Apple Vision/Tesseract OCR; other images use local vision digest
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

Sticky-note route returns local geometry plus strict model presence/text. Multicrop model
responses are `notes: []` or `notes: [{content,confidence,languages}]`; local candidates
provide final `id`, `bbox`, `color`, and provenance.
Default remains off. Set `OCR_STICKY_RESIDENT_ENABLED=1` to enable the two-turn resident
state machine. Discovery returns indexed sheet IDs; verification returns only `keep`,
`merge`, `split`, or `drop` operations. `keep_alive` and Ollama load-duration metadata
are retained. At most two localhost requests run per media, and no remote URL is accepted.
Geometry comes only from generic physical-sheet proposals; failed, malformed, timed-out,
or disabled inference falls back to bounded local OCR. Duplicate, split, and background
records are removed without imposing a target count.
Resident empty records are rejected. Multicrop explicitly accepts an empty `notes` array.
Cache keys bind resident enabled state, route/schema version,
model, base URL, and `keep_alive`, so warm-cache transitions cannot reuse opposite-mode
results. No remote inference,
VLM correction, or fixture-specific coordinates/text is used.

Scanned-PDF OCR only runs when direct
text extraction produces no text: pages are rasterized by the PyMuPDF-capable
interpreter in the `tool--pdf` skill venv, then sent to the VLM. If Ollama or the
model is unavailable, extraction falls back to tesseract/PyMuPDF OCR. Set
`OCR_VLM_ENABLED=0` to force the classic path.

The authoritative Quick Share evaluation produced 5 true positives and 1 false
positive, and remains a technical stop because it did not pass the existing
enablement gate. Do not tune thresholds or geometry against that fixture, add
fixture-specific oracle data or hardcoded scoring, or enable resident/sticky
route from that result. Runtime does not load oracle, fixture text, coordinates,
or expected count. Until an independent held-out candidate passes every gate,
output uses ordinary local OCR.

## Future held-out evidence protocol

Future admission protocol only. New corpus must have independent source provenance:
collection date, acquisition method, custody record, and SHA-256 media hashes. It
cannot derive from or augment the Quick Share fixture. Human oracle is authored and
reviewed blind to candidate output, recording annotator, reviewer, dates, source hash,
exact text, required tokens, metadata, and relevant spans. Model-authored,
candidate-assisted, or runtime-generated oracle data is rejected.

Train/development, benchmark, and holdout material remain disjoint. Holdout IDs,
hashes, and annotations are unavailable during tuning and benchmark execution.
Manifest, fixtures, oracles, UI assertions, raw outputs, JSONL, and decision are
hash-bound immutable artifacts in a new write-once run directory. Freeze scorer and
thresholds before inference; score holdout only after candidate freeze by an
independent reviewer.

Existing harness gates are required: schema validity, exact-value safety, span safety,
UI safety, total score `>= 0.80`, candidate mean total `>= 95%` of baseline, sticky
detection/TP-FP-FN gate, synthetic text, no image parts, localhost transport, no
remote fallback, RSS below 40 GiB, no timeout/error, and reproducible input/model/
repeated output hashes identical across cold and warm runs. No target note count is allowed.

Runtime authorization also requires a separate `holdout_evaluation` binding in
the evidence artifact. That immutable artifact must hash-bind the manifest,
identify the frozen candidate, state `split: holdout` and `review: independent`,
and pass its own metrics and gates. Missing, mismatched, mutable, or failed
holdout evaluation fails closed. Fewer than two successful repeated outputs or
any output disagreement fails reproducibility and enablement.

## Signed capability admission (T3–T5)

The evidence file is not an authorization token by itself. Candidate admission
requires a schema-1 Ed25519 attestation under the configured public trust map:

```json
{
  "schema_version": 1,
  "algorithm": "Ed25519",
  "key_id": "trusted-key-id",
  "issued_at": "2026-07-23T00:00:00Z",
  "expires_at": "2026-07-24T00:00:00Z",
  "candidate_model": "qwen2.5vl:7b",
  "baseline_model": "gemma4:12b",
  "evidence_sha256": "<sha256 of canonical unsigned evidence>",
  "config_sha256": "<required hash of exact enabled config>",
  "scorer_sha256": "<sha256 of frozen scorer source>",
  "capabilities": ["visionCandidate", "stickyNotes"],
  "signature": "<base64 Ed25519 signature over canonical fields above except signature>"
}
```

Canonical encoding sorts object keys recursively and preserves array order.
Only public Ed25519 SPKI material belongs in configuration. Private signing keys
stay outside this repository. `key_id` selects the trusted key; revoked IDs are
rejected immediately. Attestations are single authorization records in this
runtime; chain fields are rejected because no persisted chain store is verified.
Expired, malformed, wrong-key, wrong-signature, or hash-mismatched attestations
fail closed.

`stickyNotes` is an explicit capability, not implied by a valid evidence file.
Without it, sticky classification follows ordinary deterministic OCR. Candidate
vision separately requires `visionCandidateEnabled=true`; both flags remain
default-off in production config.

The verifier binds candidate/baseline identity, evidence hash, manifest/result/
oracle references, the exact frozen scorer source hash, raw-output hashes,
repeated outputs, UI assertions, and local/no-remote gates. The frozen scorer
recomputes exact-value and span safety from bound oracle/raw inputs; total is the
mean of those two booleans. Artifact-provided `pass`, `total`, `metrics`, and
`gates` fields are claims only. Missing, changed, or malformed scorer inputs
reject the artifact.

Any failed gate stops the run and yields no enablement artifact. Infrastructure retry
requires a new immutable run ID and failure record. Quality retry requires a frozen
candidate and genuinely new independent holdout; retry-until-pass on same holdout is
prohibited.

Enablement requires frozen candidate identity, schema-v2 evidence, independent
held-out review, manifest/result hashes, every gate true, matching `evidenceSha256`
and `configSha256`, exact config flag, and successful `verify_vision.mjs` plus
`verify_media_guard_config.mjs`. Evidence alone never enables a candidate. T2
performs no enablement.

Exact rollback: set `visionCandidateEnabled=false` and `ocrCorrection=false`, restore
`visionModel=gemma4:12b`, clear `evidencePath`, `evidenceSha256`, attestation key
references, revocation, config, and scorer bindings, restart opencode, then rerun
`bun tests/verify_evidence_gate.mjs`, `bun tests/verify_rollback.mjs`, and
`bun tests/verify_media_guard_config.mjs`. Candidate cache entries cannot affect
baseline path. Do not issue a replacement attestation until trust-key rotation
and evidence/config hashes are independently reviewed.

Optional environment variables:

- `OCR_VLM_ENABLED` — default `0`; set `1` to enable the one-request local content-assignment path.
- `OCR_VLM_BASE_URL` — default `http://127.0.0.1:11434`.
- `OCR_VLM_MODEL` — default `qwen2.5vl:32b`.
- `OCR_VLM_NUM_CTX` — default `16384`.
- `OCR_PDF_MAX_PAGES` — default `20`; maximum scanned-PDF pages OCR'd.
- `GRANITE_DOCLING_ENABLED` — default `1`; set `0` to disable local Granite-Docling classification.
- `GRANITE_DOCLING_BASE_URL` — default `http://127.0.0.1:11434`.
- `GRANITE_DOCLING_MODEL` — default `granite-docling:latest`; used only when the exact tag appears in Ollama `/api/tags`.
- `MEDIA_NOTE_TILED_PROPOSALS` — default `0`; opt-in overlapping color/edge proposal tiles.
- `MEDIA_NOTE_TILE_SIZE` — default `768`; tile edge in source pixels before bounded processing.
- `MEDIA_NOTE_TILE_OVERLAP` — default `0.20`; overlap fraction between adjacent tiles.
- `OCR_STICKY_MULTICROP_ENABLED` — default `0`; enables bounded resident qwen2.5vl:32b crop requests.
- `OCR_STICKY_MULTICROP_MAX_REQUESTS` — default `8`; hard cap on crop requests per image.

Tiled proposals retain source provenance and are converted to global source-image pixels
before grid-safe dedupe. Their proposal cap is 32. Tile recall is measured before any
content assignment request. The runtime never loads oracle coordinates or counts.

Multicrop is a separate default-off route. It ranks and NMS-deduplicates bounded local
physical-sheet proposals, capped at `OCR_STICKY_MULTICROP_MAX_REQUESTS`; uniform tile
geometry is not used. Each proposal crop is sent only to the already-resident localhost
model, with at most one presence/text finding per proposal. Response schema is exactly
`notes: []` or `notes: [{content, confidence, languages}]`; coordinates, IDs, colors,
aliases, and unknown fields are rejected. Local proposals own final geometry: each finding
maps to exactly one proposal's box, ID, color, and provenance. Dedupe operates on local
proposal boxes; model text never creates, expands, or merges geometry.
Malformed values produce bounded field/predicate diagnostics without OCR content.
Unknown or conflicting fields remain rejected. Any unavailable model, malformed response,
deadline, or request failure falls back to local OCR, never to indexed resident routing. The
result metadata emits `route: multicrop` on success and `route: multicrop-fallback` when local
OCR is used.
On fallback, `meta.request_count` is the number of requests attempted, incremented before each
request call. `meta.failure_code` is bounded to `MULTICROP_REQUEST_EXCEPTION`,
`MULTICROP_CROP_FAILURE`, `MULTICROP_DEADLINE`, `MULTICROP_SCHEMA_INVALID`, or
`MULTICROP_ERROR`; `meta.failure_reason` is bounded to 400 characters. Crop and pre-request
deadline failures therefore report zero attempted requests; request exceptions, timeouts, and
schema failures report the request already attempted. The nested `meta.multicrop` record
preserves the same failure metadata and crop cap context. Schema failures also retain only a
 bounded `response_shape` (top-level keys, array lengths, and first two item shapes), never OCR
 content. They also retain privacy-safe geometry evidence. No OCR text is included.

Successful and fallback CLI artifacts bind `provenance.audit` to input SHA-256, MIME, byte
length, image dimensions when available, and the absolute extractor source path and SHA-256.
When Pillow cannot decode HEIC/HEIF on macOS, audit dimensions use the same timeout-bounded
`sips` probe as conversion, so source dimensions cannot diverge between audit and extraction.
Disable `OCR_STICKY_MULTICROP_ENABLED` to restore the legacy route; no migration is required.

Granite-Docling availability is fail-closed: media-guard does not pull models, guess aliases, or call remote inference. Verify locally with:

```bash
ollama list
curl -fsS http://127.0.0.1:11434/api/tags
```

## Local vision benchmark rollback

### Sticky multicrop benchmark

The one-shot runner pre-inference binds the authoritative HEIC fixture and oracle by
SHA-256, verifies fixture dimensions, and records the binding in the manifest. It creates a
fresh UUID-backed `run_id` and rejects extractor output whose embedded audit hashes do not
match the current fixture/source before scoring; stale artifacts cannot be reused. It hard-binds
`--ocr-engine sticky-notes`, `--no-cache`, both
sticky route flags, `qwen2.5vl:32b`, and `http://127.0.0.1:11434`. Fixture,
oracle, and extractor hashes plus argv, relevant environment, latency, RSS,
request metadata, languages, and score are written to a new output directory.
Before inference, runner invokes `extract.py --audit-only` with local model paths disabled.
Preflight must report current fixture/source hashes and dimensions; otherwise no model request
is created. Direct current-source audit:

```bash
python3 media/extract.py /private/path/authoritative.heic --kind image --audit-only
```
The run is accepted only when `route=multicrop` and
`tool=qwen2.5vl-resident-multicrop`; wrong route/tool is a coded rejection.

```bash
python3 tests/run_sticky_multicrop_benchmark.py \
  --fixture /private/path/quick-share.jpg \
  --oracle /private/path/oracle-19.json \
  --out /private/path/sticky-multicrop-run
```

Run mock routing coverage before the single real call:

```bash
python3 -m unittest tests/test_run_sticky_multicrop_benchmark.py
```

Do not retry a real inference after route, tool, request, timeout, or metric
failure. Oracle remains external and must contain exactly 19 notes.

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
