// media-guard — keep unsupported media attachments (PDF / audio / video / image)
// from crashing a session, and inline locally-extracted text instead.
//
// Problem: when the user attaches/references a PDF (or audio, video, image),
// opencode builds a file part with that mime and ships the raw bytes to the
// provider. Providers that reject the media type fail the ENTIRE request with
// e.g. 'file part media type application/pdf' functionality not supported, which
// makes the session unusable.
//
// Fix: in experimental.chat.messages.transform (the last stop before the LLM
// call) swap every file part whose mime is in the unsupported set for a
// synthetic TEXT part. A deterministic local dispatcher (media/extract.py)
// produces the text: pdftotext/PyMuPDF/pypdf for PDF, whisper for audio/video,
// tesseract for images. Results are cached (in-memory here + on disk in
// extract.py). If extraction is unavailable we emit a pointer note routing the
// agent to the matching skill (tool--pdf / tool--transcribe). No media bytes
// ever reach the remote, so the session keeps working.
//
// This generalizes the former pdf-guard.ts and consolidates the extraction
// logic that used to live in the tool--pdf and tool--transcribe skills.
//
// Also registers a `media_extract` tool so the agent can parse a media file
// on demand (e.g. a larger model, or the full untruncated text).

import { tool, type Plugin } from "@opencode-ai/plugin"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { writeFileSync, statSync, mkdtempSync } from "node:fs"

type Options = {
  // mime patterns to intercept. "*" wildcard suffix supported (e.g. "audio/*").
  mimes?: string[]
  // also intercept images (OCR). Off by default — many sessions want to *see*
  // images via vision models rather than OCR them.
  ocrImages?: boolean
  // Max characters of extracted text to inline per file.
  maxChars?: number
  // Whisper model for audio/video transcription.
  model?: string
  // Per-file extraction timeout (seconds) on the send-path.
  timeoutSec?: number
}

const DEFAULT_MIMES = ["application/pdf", "audio/*", "video/*"]
const EXTRACT = new URL("./media/extract.py", import.meta.url).pathname

// Map media kind -> the skill that handles the richer, non-extraction work.
const SKILL_FOR: Record<string, string> = {
  pdf: "tool--pdf",
  audio: "tool--transcribe",
  video: "tool--transcribe",
  image: "tool--pdf", // ocr_extract lives in tool--pdf
}

function mimeMatches(mime: string, patterns: string[]): boolean {
  const m = (mime || "").toLowerCase()
  for (const p of patterns) {
    const pat = p.toLowerCase()
    if (pat.endsWith("/*")) {
      if (m.startsWith(pat.slice(0, -1))) return true // "audio/" prefix
    } else if (m === pat) {
      return true
    }
  }
  return false
}

function kindForMime(mime: string): string {
  const m = (mime || "").toLowerCase()
  if (m === "application/pdf") return "pdf"
  if (m.startsWith("audio/")) return "audio"
  if (m.startsWith("video/")) return "video"
  if (m.startsWith("image/")) return "image"
  return "unknown"
}

export const MediaGuardPlugin: Plugin = async ({ $ }, opts: Options = {}) => {
  const mimes = [...(opts.mimes ?? DEFAULT_MIMES)]
  if (opts.ocrImages) mimes.push("image/*")
  const maxChars = opts.maxChars ?? 60_000
  const model = opts.model ?? "base"
  const timeoutSec = opts.timeoutSec ?? 180

  // in-memory result cache: path+mtime+size -> note text
  const cache = new Map<string, string>()
  const MAX_CACHE = 64

  // Resolve a FilePart's url/source to a local file path.
  const resolvePath = (part: any): string | null => {
    const src = part?.source
    if (src && typeof src.path === "string" && src.path) return src.path
    const url: string = part?.url ?? ""
    if (!url) return null
    if (url.startsWith("file://")) {
      try { return decodeURIComponent(new URL(url).pathname) } catch { return null }
    }
    if (url.startsWith("data:")) {
      const comma = url.indexOf(",")
      if (comma < 0) return null
      const meta = url.slice(5, comma)
      const body = url.slice(comma + 1)
      if (!/base64/i.test(meta)) return null
      try {
        const dir = mkdtempSync(join(tmpdir(), "media-guard-"))
        const safe = (part?.filename || "attachment").replace(/[^\w.-]+/g, "_") || "attachment"
        const p = join(dir, safe)
        writeFileSync(p, Buffer.from(body, "base64"))
        return p
      } catch { return null }
    }
    if (url.startsWith("/")) return url
    return null // http(s) or anything not locally reachable
  }

  // Call extract.py; returns parsed JSON or null on hard failure.
  const runExtract = async (path: string, mime: string, max: number): Promise<any | null> => {
    const r = await $`python3 ${EXTRACT} ${path} --mime ${mime} --max-chars ${max} --timeout ${timeoutSec} --model ${model}`
      .quiet().nothrow()
    const out = String(r.stdout).trim()
    if (!out) return null
    try { return JSON.parse(out) } catch { return null }
  }

  const skillHint = (kind: string): string => {
    const s = SKILL_FOR[kind]
    return s ? `Load the \`${s}\` skill to process it.` : "No local extractor is available for this file."
  }

  // Build the replacement text for one intercepted file part.
  const buildNote = async (part: any, mime: string): Promise<string> => {
    const kind = kindForMime(mime)
    const path = resolvePath(part)
    const label = part?.filename || path || part?.url || "attachment"
    const header =
      `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the ` +
      `model — this provider rejects ${mime} file parts (sending it would break the turn).`

    if (!path) {
      return `${header}\nThe file isn't reachable as a local path, so its content ` +
        `couldn't be extracted automatically. ${skillHint(kind)}`
    }

    // in-memory cache
    let ckey = path
    try { const st = statSync(path); ckey = `${path}:${st.mtimeMs}:${st.size}:${maxChars}:${model}` } catch {}
    const hit = cache.get(ckey)
    if (hit) return hit

    const res = await runExtract(path, mime, maxChars)
    let note: string
    if (res && res.status === "ok" && res.text) {
      const trunc = res.truncated
        ? `\n\n…[truncated to ${maxChars} chars of ${res.full_chars}. For the full ` +
          `content use the media_extract tool or load the \`${SKILL_FOR[kind] ?? "relevant"}\` skill on ${path}.]`
        : ""
      const via = res.tool ? ` via ${res.tool}` : ""
      note = `${header}\nIts extracted text is inlined below${via}.\nFile: ${path}\n\n` +
        `----- BEGIN EXTRACTED ${kind.toUpperCase()} TEXT -----\n${res.text}` +
        `\n----- END EXTRACTED ${kind.toUpperCase()} TEXT -----${trunc}`
    } else {
      const why = res?.detail ? ` (${res.detail})` : ""
      note = `${header}\nLocal extraction was unavailable${why}. ${skillHint(kind)} ` +
        `File: ${path}`
    }
    cache.set(ckey, note)
    if (cache.size > MAX_CACHE) cache.delete(cache.keys().next().value as string)
    return note
  }

  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      try {
        for (const msg of output.messages ?? []) {
          const parts = msg.parts as any[]
          if (!Array.isArray(parts) || parts.length === 0) continue
          let touched = false
          for (let i = 0; i < parts.length; i++) {
            const p = parts[i]
            if (p?.type === "file" && typeof p.mime === "string" && mimeMatches(p.mime, mimes)) {
              parts[i] = {
                id: p.id,
                sessionID: p.sessionID,
                messageID: p.messageID,
                type: "text",
                text: await buildNote(p, p.mime),
                synthetic: true,
              }
              touched = true
            }
          }
          if (touched) msg.parts = parts
        }
      } catch (e) {
        console.error("[media-guard] transform failed, leaving messages untouched:", e)
      }
    },

    tool: {
      media_extract: tool({
        description:
          "Extract text from a local media file (PDF, audio, video, or image) " +
          "using deterministic local tools (PyMuPDF/pdftotext/pypdf, whisper, " +
          "tesseract). Use for on-demand parsing, a larger transcription model, " +
          "or to get the full untruncated text of an attachment. Returns the " +
          "extracted text.",
        args: {
          path: tool.schema.string().describe("Absolute path to the media file."),
          mime: tool.schema.string().optional()
            .describe("Optional mime hint, e.g. application/pdf, audio/mpeg."),
          maxChars: tool.schema.number().int().min(1000).max(2_000_000).default(200_000)
            .describe("Max characters of text to return."),
          model: tool.schema.string().optional()
            .describe("Whisper model for audio/video (tiny|base|small|medium|large). Default base."),
        },
        async execute({ path, mime, maxChars: mc, model: md }) {
          const r = await $`python3 ${EXTRACT} ${path} --mime ${mime ?? ""} --max-chars ${mc} --timeout ${Math.max(timeoutSec, 600)} --model ${md ?? model}`
            .quiet().nothrow()
          const out = String(r.stdout).trim()
          if (!out) return "media_extract failed: no output from extractor."
          let res: any
          try { res = JSON.parse(out) } catch { return "media_extract failed to parse output:\n" + out.slice(0, 500) }
          if (res.status === "ok") {
            const t = res.truncated ? `\n[truncated to ${mc} of ${res.full_chars} chars]` : ""
            return `kind=${res.kind} tool=${res.tool} chars=${res.chars}${t}\n\n${res.text}`
          }
          return `media_extract status=${res.status}: ${res.detail ?? "unavailable"}`
        },
      }),
    },
  }
}

export default MediaGuardPlugin
