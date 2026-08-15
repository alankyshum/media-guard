# Media Guard

Materializes uploaded local files and data URLs before provider submission. It
replaces matching raw file parts with concise synthetic manifests. Content
extraction is intentionally outside this plugin; use global skills for
intentional PDF, transcription, or image OCR work.

## Manifest

Each manifest preserves the original part ID and order and includes:

- sanitized filename
- absolute local path; remote sources have no URL or pointer
- MIME and media kind
- byte size where local metadata is available
- SHA-256 for data URLs and readable local files
- source kind and concise error for unavailable remote sources

Supported sources: `source.path`, absolute local paths, `file://` URLs, base64
`data:` URLs, and remote URLs. Data URLs are written under the configured
temporary directory using a hash-derived stable filename. Only bytes required
to decode/write data URLs or compute the manifest hash are read.

Limits use explicit plugin options first, then `plugins.media_guard` in
`config/agent-runtime/agent-config.yml`, then per-setting built-in fallbacks.
The workspace block itself is required and a missing block fails initialization
loudly. Settings include `maxMaterializedBytes`, `maxFilesPerTransform`, and
`maxTotalMaterializedBytes`. The materialization directory and files are
created with `0700` and `0600` permissions and local source symlinks are
rejected.

## Content inspection

Inspection is explicit. Load a relevant skill against the materialized path:

- images: `image--apple-vision-ocr`
- PDFs: `tool--pdf`
- audio/video: `tool--transcribe`
- archives: `media--guard-playbook` (the preprocess plugin expands them; media guard only stages and hashes them)

Archive MIME types are materialized as `media_kind: archive`, but this plugin never
opens, lists, extracts, or otherwise inspects archive contents. Expansion and member
security checks happen in the later `media-preprocess` stage.

## Verification

```sh
bun tests/verify_120.mjs
bun tests/verify_media_guard_config.mjs
npx esbuild media-guard.ts --bundle --platform=node --format=esm --outfile=/dev/null
```

The focused regression test verifies local/data/remote handling, ID/order
preservation, no content leakage, and no remote download.
