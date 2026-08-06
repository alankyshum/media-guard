---
name: media--guard-playbook
description: "Safely inspect Media Guard manifests, preprocess archive members, and route visual evidence to vision-reader."
---

# Goal

Materialize-first handling for the unified Media Guard plugin: read the manifest, use safe local extractors for every enabled media kind, and dispatch `vision-reader` for visual evidence.

# Hard rules

- Never fetch a remote URL or guess at raw media.
- Preserve the manifest JSON and use its absolute `path`.
- Treat `needs-agent` as not extracted; dispatch `vision-reader` with every listed path.
- Use `ui-ux-designer` only for active UI/design work, never passive media reading.
- Reject archive traversal/absolute paths, symlinks, non-regular files, entry counts over 200, declared uncompressed bytes over 524288000, and compression ratios over 200:1 before extraction. The post-extraction byte walk remains a defense-in-depth check.
- Never recurse into nested archives: report them as `kind=archive` with `handling=nested-archive-skipped`.
- Every kind listed in `enabledKinds` is extracted; there is no native-modality skip marker. Unsupported media is passed through to the model's own error surface.

# Manifest and operations

| Input/marker | Operation |
|---|---|
| `[media-guard attachment manifest]` + JSON | Read `filename`, `path`, `mime`, `media_kind`, `size`, `sha256`, and `error`. A null path/error means materialization failed. |
| `[media-preprocess extracted: ...]` | Use the bounded inline extraction directly. |
| `[media-preprocess archive: ...]` + JSON `entries` | Read every absolute member path, MIME, kind, and handling bucket. |
| `handling=auto-preprocessed` | Use the following auto-extracted text directly. |
| `handling=needs-agent` | Dispatch `vision-reader` via `task` against each path before answering visual questions. |
| `[media-preprocess pdf-pages: ...]` | Read the listed absolute WebP page paths and dispatch `vision-reader` via `task` for visual PDF questions. Capped by `maxPdfPageImages` (50). |
| `[media-preprocess video-keyframes: ...]` | Read the listed absolute WebP keyframe paths and dispatch `vision-reader` via `task` for visual video questions. Capped by `maxVideoKeyframes` (20). |
| `pdf-pages-failed` / `video-keyframes-failed` | Page-image or keyframe extraction failed; use available inline text/transcript and report the failure rather than guessing. |

| Media | Exact command |
|---|---|
| PDF | `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pdf_tool.py read-text <pdf> --format json` |
| Image OCR | `${CLAUDE_PLUGIN_ROOT}/scripts/apple-vision-ocr <image>` |
| Audio/video | `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/transcribe_audio.py <path> --backend auto --model turbo --formats txt --output-dir <dir>` |
| Office document | `npx -y @firecrawl/anydoc <file>` (or the `anydoc` binary / `MEDIA_GUARD_ANYDOC`) |
| ZIP | `unzip -Z1 <archive>` then `unzip -o <archive> -d <private-dir>` |
| TAR/gzip/bzip2/xz | `tar -tf <archive>` then `tar -xf <archive> -C <private-dir>` |
| 7z/RAR | `7z l -slt <archive>` then `7z x -y <archive> -o<private-dir>` when installed |

Archive extraction is cached under `<cache>/<sha256>.archive/` with private directory/file permissions. Text, PDF, audio, video, and document members are auto-preprocessed; images and other visual members are routed, not OCR'd inline. Top-level PDF extraction also emits per-page WebP images at q75 with a 1568px long edge, and top-level video extraction emits scene-guided WebP keyframes. The strict no-nested-archive policy is deliberate: nested archive members are surfaced but never expanded.

# Troubleshooting

| Symptom | Fix |
|---|---|
| `archive failed` | Check the safe tool-availability, corrupt archive, traversal, and configured limit marker; do not retry with an unsafe path. |
| `needs-agent` present | Dispatch `vision-reader` with the concrete absolute paths and the user's question. |
| Empty extraction | Report the uncertain/empty marker; do not infer content. |
| Missing transcript/PDF output | Run `scripts/setup.sh`, verify the configured virtual environment and output directory, then report failure. |
