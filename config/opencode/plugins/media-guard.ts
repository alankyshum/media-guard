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
import { join, dirname } from "node:path"
import { writeFileSync, statSync, mkdtempSync, readFileSync, realpathSync } from "node:fs"
import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import { parse as parseYaml } from "yaml"

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
  // Media kinds routed to the cheap agent. Default ["image"].
  agentKinds?: string[]
  // Model identifier for the cheap agent. Default: the code-oc agent's model from config/agent-runtime/agent-config.yml (hardcoded fallback if unreachable).
  agentModel?: string
  // Model variant for the cheap agent. Default: the code-oc agent's effort from config/agent-runtime/agent-config.yml (hardcoded fallback if unreachable).
  agentVariant?: string
  // Timeout (seconds) for cheap agent calls. Default 300.
  agentTimeoutSec?: number
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

const MAX_AGENT_FRAMES = 16

function hashStr(s: string): string {
  let hash = 5381
  for (let i = 0; i < s.length; i++) {
    hash = ((hash << 5) + hash) + s.charCodeAt(i)
  }
  return (hash >>> 0).toString(36)
}

function latestUserText(messages: any[]): string {
  try {
    if (!Array.isArray(messages)) return ""
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i]
      if (!msg) continue
      const role = msg.role ?? msg.info?.role
      if (role === "user") {
        const parts = msg.parts
        if (Array.isArray(parts)) {
          let text = ""
          for (const p of parts) {
            if (p && p.type === "text" && p.text && !p.synthetic) {
              text += p.text
            }
          }
          const trimmed = text.trim()
          return trimmed.slice(0, 2000)
        }
      }
    }
  } catch {
    // try/catch -> "" on any error
  }
  return ""
}

function imagePrompt(goal: string): string {
  let p = "Extract the attached media into a compact JSON digest for a downstream " +
    "agent that will NOT see the original file. Output ONLY minified JSON — no " +
    "prose, no code fences. Schema: " +
    "{\"content_type\":string,\"summary\":string,\"key_metadata\":{byte-exact ids/codes/amounts/dates/dimensions/names/urls},\"relevant_spans\":[{\"quote\":verbatim string,\"location\":short where-hint}],\"full_text\":string}. " +
    "Preserve exact strings; never guess."
  if (goal) {
    p += `\nThe downstream agent's current goal is:\n"""\n${goal}\n"""\n` +
      "Prioritize relevant_spans that answer this goal, while still filling all schema fields."
  }
  return p
}

function videoPrompt(goal: string, transcript: string): string {
  let tText = transcript || ""
  if (tText.trim() === "") {
    tText = "(no transcript / silent or audioless video)"
  } else if (tText.length > 12000) {
    tText = tText.slice(0, 12000) + "…[truncated]"
  }

  let p = "Extract the attached video into a compact JSON digest for a downstream " +
    "agent that will NOT see the original video. N ordered scene-sampled keyframes are " +
    "ATTACHED (in order) plus the audio transcript is inline. Output ONLY minified JSON — no " +
    "prose, no code fences. Schema: " +
    "{\"content_type\":\"video\",\"summary\":string,\"key_metadata\":{byte-exact values visible on-frame or spoken},\"timeline\":[{\"frame\":int(1-based),\"visual\":short desc,\"transcript_excerpt\":string|null}],\"relevant_spans\":[{\"quote\":verbatim,\"location\":\"frame N\"|\"transcript\"}],\"full_transcript_included\":bool}. " +
    "Preserve exact strings; never guess.\n\n" +
    `TRANSCRIPT:\n${tText}`

  if (goal) {
    p += `\n\nThe downstream agent's current goal is:\n"""\n${goal}\n"""\n` +
      "Prioritize relevant_spans that answer this goal, while still filling all schema fields."
  }
  return p
}

// The cheap-agent model/effort default to the code-oc agent defined in the
// single source of truth (config/agent-runtime/agent-config.yml), so a model
// swap there propagates here without editing this plugin. Resolved via the
// plugin's real path (opencode loads it through a symlink). Falls back to a
// hardcoded pair if the SOT is unreachable (e.g. plugin used outside dotfiles).
const REAL_PLUGIN_PATH = (() => {
  const p = fileURLToPath(import.meta.url)
  try { return realpathSync(p) } catch { return p }
})()
const SOT_YML = join(dirname(REAL_PLUGIN_PATH), "..", "..", "agent-runtime", "agent-config.yml")

function resolveCheapAgent(): { model?: string; effort?: string } {
  try {
    const spec: any = parseYaml(readFileSync(SOT_YML, "utf8"))
    const agent = spec?.agents?.["code-oc"]
    return { model: agent?.model, effort: agent?.effort }
  } catch {
    return {}
  }
}

export const MediaGuardPlugin: Plugin = async ({ $ }, opts: Options = {}) => {
  const agentKinds = new Set(opts.agentKinds ?? ["image"])
  const cheapAgent = resolveCheapAgent()
  const agentModel = opts.agentModel ?? cheapAgent.model ?? "github-copilot/gemini-3.5-flash"
  const agentVariant = opts.agentVariant ?? cheapAgent.effort ?? "medium"
  const agentTimeoutMs = (opts.agentTimeoutSec ?? 300) * 1000

  const mimes = [...(opts.mimes ?? DEFAULT_MIMES)]
  if (opts.ocrImages || agentKinds.has("image")) {
    if (!mimes.includes("image/*")) {
      mimes.push("image/*")
    }
  }
  for (const k of agentKinds) {
    let p: string | null = null
    if (k === "audio") p = "audio/*"
    else if (k === "video") p = "video/*"
    else if (k === "pdf") p = "application/pdf"
    if (p && !mimes.includes(p)) {
      mimes.push(p)
    }
  }

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

  const extractJson = (raw: string): string | null => {
    let content = raw.trim()
    const fenceMatch = content.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)
    if (fenceMatch) {
      content = fenceMatch[1]
    } else {
      const firstBrace = content.indexOf("{")
      const lastBrace = content.lastIndexOf("}")
      if (firstBrace !== -1 && lastBrace !== -1 && lastBrace >= firstBrace) {
        content = content.slice(firstBrace, lastBrace + 1)
      }
    }
    try {
      return JSON.stringify(JSON.parse(content))
    } catch {
      return null
    }
  }

  const runAgent = (paths: string[], promptText: string): Promise<string | null> => {
    return new Promise((resolve) => {
      const clean = (s: string) => s.replace(/\0/g, "")
      let resolved = false
      const args = [
        "run",
        clean(promptText),
        "--pure",
        "-m",
        clean(agentModel),
        "--variant",
        clean(agentVariant)
      ]
      for (const p of paths) {
        args.push("-f", clean(p))
      }

      try {
        const child = spawn("opencode", args, {
          stdio: ["ignore", "pipe", "ignore"]
        })

        let stdout = ""
        child.stdout.on("data", (chunk) => {
          stdout += chunk.toString()
        })

        const timer = setTimeout(() => {
          if (!resolved) {
            resolved = true
            child.kill("SIGKILL")
            resolve(null)
          }
        }, agentTimeoutMs)

        child.on("error", () => {
          if (!resolved) {
            resolved = true
            clearTimeout(timer)
            resolve(null)
          }
        })

        child.on("exit", (code) => {
          if (!resolved) {
            resolved = true
            clearTimeout(timer)
            if (code === 0) {
              resolve(stdout)
            } else {
              resolve(null)
            }
          }
        })
      } catch {
        if (!resolved) {
          resolved = true
          resolve(null)
        }
      }
    })
  }

  const extractNote = (res: any, kind: string, path: string, header: string): string => {
    if (res && res.status === "ok" && res.text) {
      const trunc = res.truncated
        ? `\n\n…[truncated to ${maxChars} chars of ${res.full_chars}. For the full ` +
          `content use the media_extract tool or load the \`${SKILL_FOR[kind] ?? "relevant"}\` skill on ${path}.]`
        : ""
      const via = res.tool ? ` via ${res.tool}` : ""
      return `${header}\nIts extracted text is inlined below${via}.\nFile: ${path}\n\n` +
        `----- BEGIN EXTRACTED ${kind.toUpperCase()} TEXT -----\n${res.text}` +
        `\n----- END EXTRACTED ${kind.toUpperCase()} TEXT -----${trunc}`
    } else {
      const why = res?.detail ? ` (${res.detail})` : ""
      return `${header}\nLocal extraction was unavailable${why}. ${skillHint(kind)} ` +
        `File: ${path}`
    }
  }

  // Build the replacement text for one intercepted file part.
  const buildNote = async (part: any, mime: string, userGoal: string): Promise<string> => {
    const kind = kindForMime(mime)
    const path = resolvePath(part)
    const label = part?.filename || path || part?.url || "attachment"
    const viaAgent = agentKinds.has(kind)
    const initialHeader = viaAgent
      ? `[media-guard] A ${kind} file ("${label}") was processed by a cheaper model instead of being sent to you as raw bytes (saves tokens; byte-exact values are preserved in key_metadata below).`
      : `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the ` +
        `model — this provider rejects ${mime} file parts (sending it would break the turn).`

    if (!path) {
      return `${initialHeader}\nThe file isn't reachable as a local path, so its content ` +
        `couldn't be extracted automatically. ${skillHint(kind)}`
    }

    // in-memory cache
    let ckey = path
    try { const st = statSync(path); ckey = `${path}:${st.mtimeMs}:${st.size}:${maxChars}:${model}:${mime}` } catch {}
    if (viaAgent) {
      ckey += `:${agentModel}:${agentVariant}:${hashStr(userGoal)}`
    }
    const hit = cache.get(ckey)
    if (hit) return hit

    const cacheSetAndEvict = (key: string, note: string): string => {
      cache.set(key, note)
      if (cache.size > MAX_CACHE) {
        cache.delete(cache.keys().next().value as string)
      }
      return note
    }

    // IMAGE agent branch
    if (kind === "image" && agentKinds.has("image") && path) {
      const digest = await runAgent([path], imagePrompt(userGoal))
      const json = digest ? extractJson(digest) : null
      if (json !== null) {
        const header = `[media-guard] An image ("${label}") was distilled to a JSON digest by a cheaper model instead of being sent as raw bytes (byte-exact values preserved in key_metadata).`
        const note = header + `\nIf you need the original, use the media_extract tool or read the file directly.\nFile: ${path}\n\n----- BEGIN MEDIA DIGEST (JSON) -----\n${json}\n----- END MEDIA DIGEST (JSON) -----`
        return cacheSetAndEvict(ckey, note)
      }
      // On failure fall through to deterministic OCR
      const fallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
      const res = await runExtract(path, mime, maxChars)
      const note = extractNote(res, kind, path, fallbackHeader)
      return cacheSetAndEvict(ckey, note)
    }

    // VIDEO branch
    if (kind === "video" && agentKinds.has("video") && path) {
      const res = await runExtract(path, mime, maxChars) // frames + transcript
      const frames = Array.isArray(res?.meta?.frames) ? res.meta.frames : []
      const transcript = (res?.text || "")
      if (frames.length) {
        const useFrames = frames.slice(0, MAX_AGENT_FRAMES)
        const digest = await runAgent(useFrames, videoPrompt(userGoal, transcript))
        const json = digest ? extractJson(digest) : null
        if (json !== null) {
          const header = `[media-guard] A video ("${label}") was distilled by a cheaper multimodal model — ${frames.length} scene-sampled keyframes fused with its audio transcript — instead of being sent as raw bytes. Full transcript, frames, and the original video remain on disk.`
          const note = header + `\nUse the media_extract tool on the File path for the COMPLETE transcript, or read individual frames.\nFile: ${path}\nFrames dir: ${dirname(useFrames[0])}\n\n----- BEGIN VIDEO DIGEST (JSON) -----\n${json}\n----- END VIDEO DIGEST (JSON) -----`
          return cacheSetAndEvict(ckey, note)
        }
      }
      // fusion unavailable → fall back to deterministic transcript inline:
      const videoFallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
      const note = extractNote(res, kind, path, videoFallbackHeader)
      return cacheSetAndEvict(ckey, note)
    }

    // Otherwise (pdf / audio / non-agent kinds)
    const fallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
    const res = await runExtract(path, mime, maxChars)
    const note = extractNote(res, kind, path, fallbackHeader)
    return cacheSetAndEvict(ckey, note)
  }

  return {
    "experimental.chat.messages.transform": async (_input, output) => {
      let userGoal = ""
      try { userGoal = latestUserText(output.messages) } catch { userGoal = "" }
      for (const msg of output.messages ?? []) {
        let parts: any[]
        try {
          parts = msg.parts as any[]
          if (!Array.isArray(parts) || parts.length === 0) continue
        } catch { continue }
        let touched = false
        for (let i = 0; i < parts.length; i++) {
          const p = parts[i]
          let isMatch = false
          try {
            isMatch = p?.type === "file" && typeof p.mime === "string" && mimeMatches(p.mime, mimes)
          } catch { isMatch = false }
          if (!isMatch) continue
          // Once a media part matches we MUST replace it — never leave raw bytes
          // in the turn (fail SAFE, not fail open).
          let text: string
          try {
            text = await buildNote(p, p.mime, userGoal)
          } catch (e) {
            const label = p?.filename || p?.url || "attachment"
            text = `[media-guard] A media file ("${label}", ${p.mime}) was attached but its content could not be processed (${e instanceof Error ? e.message : String(e)}). It was NOT sent to the model, to avoid leaking raw bytes or breaking the turn. If you need its content, use the media_extract tool on its local path.`
          }
          try {
            parts[i] = {
              id: p.id,
              sessionID: p.sessionID,
              messageID: p.messageID,
              type: "text",
              text,
              synthetic: true,
            }
            touched = true
          } catch (e) {
            console.error("[media-guard] failed to swap media part:", e)
          }
        }
        if (touched) {
          try { msg.parts = parts } catch (e) { console.error("[media-guard] failed to assign parts:", e) }
        }
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
