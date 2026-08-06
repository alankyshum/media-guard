# Media Guard

Media Guard materializes local attachments into private, permission-restricted staging, emits a bounded manifest, and preprocesses PDFs, images, audio, video, and archives. It keeps text extraction and visual evidence routing separate: rendered PDF pages and scene-guided video keyframes are handed to `vision-reader` rather than guessed at by the plugin.

## Install for opencode

From this repository, install the Python dependencies once:

```sh
./external/media-guard/scripts/setup.sh
```

Add the plugin path to `config/opencode/opencode.jsonc` (the dotfiles configuration already does this):

```jsonc
{
  "plugin": [
    "../../external/media-guard/media-guard.ts"
  ]
}
```

For another checkout, replace the relative path with the absolute path to `media-guard.ts`. The plugin resolves its vendored scripts relative to that file, not relative to the current working directory.

## Install for Claude Code

Run Claude Code with the local plugin directory:

```sh
claude --plugin-dir /absolute/path/to/dotfiles/external/media-guard
```

The Claude plugin manifest is `.claude-plugin/plugin.json`; its skill is auto-discovered from `skills/media--guard-playbook/SKILL.md`.

## Prerequisites

- **ffmpeg and ffprobe:** required for video keyframes and audio/video preprocessing. Put both on `PATH`, or set `MEDIA_GUARD_FFMPEG` and `MEDIA_GUARD_FFPROBE`.
- **Python 3:** required. `scripts/setup.sh` creates `external/media-guard/.venv` and installs PyMuPDF, Pillow, and a Whisper backend. The plugin uses that venv when present, otherwise `python3` from `PATH`.
- **Tesseract:** optional, but required for scanned-PDF OCR. On macOS, install with `brew install tesseract tesseract-lang`, or set `MEDIA_GUARD_TESSERACT` to its executable. Text PDFs do not need Tesseract.
- **Apple Vision OCR:** optional and macOS-only. It requires `swiftc` and the Vision framework. On other systems image OCR degrades clearly with an extractor error; visual files can still be routed to `vision-reader`.
- Archive tools are optional by archive type: `unzip`, `tar`, and optionally `7z` for 7z/RAR.

## Configuration reference

The existing dotfiles runtime supplies these values under `media_guard:` in `config/agent-runtime/agent-config.yml`. Plugin options take precedence over workspace configuration.

| Setting | Default | Meaning |
|---|---:|---|
| `maxMaterializedBytes` | `104857600` | Maximum bytes for one staged attachment. |
| `maxMaterializedFilesPerTransform` | `64` | Maximum top-level attachments in one transform during materialization/guard staging. |
| `maxTotalMaterializedBytes` | `524288000` | Maximum aggregate staged bytes in one transform. |
| `maxExtractedChars` | `200000` | Maximum inline extracted characters. |
| `timeoutMs` | `300000` | Extractor timeout in milliseconds. |
| `enabledKinds` | `pdf,image,audio,video,text,document,archive` | Media kinds eligible for preprocessing. |
| `maxExtractedFilesPerTransform` | `16` | Maximum files preprocessed from manifests/archives. |
| `maxArchiveEntries` | `200` | Maximum archive members. |
| `maxArchiveBytes` | `524288000` | Maximum declared and post-extraction archive bytes. |
| `maxCompressionRatio` | `200` | Maximum declared-size to archive-size ratio. |
| `maxPdfPageImages` | `50` | Maximum rendered PDF pages. |
| `maxVideoKeyframes` | `20` | Maximum extracted video frames. |
| `maxTextBytes` | `409600` | Maximum bytes read from a text attachment. |
| `maxTextChars` | `100000` | Maximum characters extracted from a text attachment. |
| `materializationDir` | `${TMPDIR}/opencode-media-guard` | Private attachment staging directory. |
| `cacheDir` | `${TMPDIR}/opencode-media-preprocess` | Private extraction cache. |

Environment overrides:

| Variable | Purpose |
|---|---|
| `MEDIA_GUARD_PYTHON` | Explicit Python executable; otherwise the repo-local `.venv/bin/python`, then `python3` from `PATH`. |
| `MEDIA_GUARD_ANYDOC` | Explicit anydoc executable; otherwise `anydoc` from `PATH`, then `npx -y @firecrawl/anydoc`. |
| `MEDIA_GUARD_FFMPEG` | Explicit ffmpeg executable for video keyframes. |
| `MEDIA_GUARD_FFPROBE` | Explicit ffprobe executable for video duration. |
| `MEDIA_GUARD_TESSERACT` | Explicit Tesseract executable for scanned-PDF OCR. |
| `MEDIA_GUARD_VENV` | Alternate venv location used by `scripts/setup.sh`. |
| `MEDIA_GUARD_BOOTSTRAP_PYTHON` | Python used by setup to create the venv. |

## Security model

Staging and cache directories are created as private `0700` directories; materialized and generated files are `0600`. Source symlinks, staging-directory symlinks, archive symlinks, non-regular files, absolute archive names, drive-qualified names, and `..` traversal members are rejected or removed. Archive expansion is bounded by 200 entries, 500 MiB declared/post-extraction bytes, and a 200:1 compression ratio. Nested archives are reported but never recursively expanded. Temporary files are created with exclusive creation and renamed into place, and cached work requires a completion marker before reuse.

## Provenance

The video keyframe extractor was written independently from public ffmpeg documentation and first principles. It does not derive from, copy, or port any AGPL-licensed project. The algorithm uses ffmpeg `select='gt(scene,T)',metadata=print` scene detection parsing `pts_time`, scene-guided timestamp selection with midpoint insertion for long segments, `-ss` extraction, and Pillow WebP conversion.
