// Materialize attachments. Never inspect media content beyond staging/hash bytes.
import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import { accessSync, chmodSync, createReadStream, existsSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, statSync, closeSync, unlinkSync, writeSync, writeFileSync, constants as fsConstants } from "node:fs"
import { basename, dirname, extname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { tmpdir } from "node:os"

type Limits = { maxMaterializedBytes: number; maxFilesPerTransform: number; maxTotalMaterializedBytes: number }
type Options = Partial<Limits> & { mimes?: string[]; materializationDir?: string }
const DEFAULT_MIMES = ["image/*", "application/pdf", "audio/*", "video/*"]
const FALLBACKS: Limits = { maxMaterializedBytes: 100 * 1024 * 1024, maxFilesPerTransform: 64, maxTotalMaterializedBytes: 500 * 1024 * 1024 }
const PLUGIN_PATH = realpathSync(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(dirname(PLUGIN_PATH), "../..")
const CONFIG_PATH = join(REPO_ROOT, "config/agent-runtime/agent-config.yml")
const DEFAULT_DIR = join(tmpdir(), "opencode-media-guard")

function positive(value: unknown, fallback: number): number { return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback }
function workspaceLimits(): Partial<Limits> {
  try {
    const text = readFileSync(CONFIG_PATH, "utf8")
    const section = text.match(/^media_guard:\s*\n((?:^[ \t]+[^\n]*\n?)+)/m)?.[1] ?? ""
    const out: Partial<Limits> = {}
    for (const key of Object.keys(FALLBACKS) as (keyof Limits)[]) {
      const match = section.match(new RegExp(`^\\s*${key}:\\s*(\\d+)\\s*$`, "m"))
      if (match) out[key] = Number(match[1])
    }
    return out
  } catch { return {} }
}
function limits(opts: Options): Limits {
  const workspace = workspaceLimits()
  return {
    maxMaterializedBytes: positive(opts.maxMaterializedBytes, positive(workspace.maxMaterializedBytes, FALLBACKS.maxMaterializedBytes)),
    maxFilesPerTransform: positive(opts.maxFilesPerTransform, positive(workspace.maxFilesPerTransform, FALLBACKS.maxFilesPerTransform)),
    maxTotalMaterializedBytes: positive(opts.maxTotalMaterializedBytes, positive(workspace.maxTotalMaterializedBytes, FALLBACKS.maxTotalMaterializedBytes)),
  }
}
function matchesMime(mime: string, patterns: string[]): boolean { return patterns.some(p => p === mime || (p.endsWith("/*") && mime.startsWith(p.slice(0, -1)))) }
function mediaKind(mime: string): string { return mime === "application/pdf" ? "pdf" : mime.startsWith("image/") ? "image" : mime.startsWith("audio/") ? "audio" : mime.startsWith("video/") ? "video" : "file" }
function safeName(value: unknown): string {
  const name = basename(typeof value === "string" ? value : "attachment").replace(/[\u0000-\u001f\u007f/\\]/g, "_").replace(/[^A-Za-z0-9._ -]/g, "_").trim()
  return name && name !== "." && name !== ".." ? name : "attachment"
}
function privateDir(dir: string): string {
  const resolved = resolve(dir)
  if (existsSync(resolved) && lstatSync(resolved).isSymbolicLink()) throw new Error("materialization directory symlinks are not accepted")
  mkdirSync(resolved, { recursive: true, mode: 0o700 })
  if (!lstatSync(resolved).isDirectory()) throw new Error("materialization path is not a directory")
  chmodSync(resolved, 0o700)
  return resolved
}
function fileUrlPath(url: string): string {
  const parsed = new URL(url)
  if (parsed.hostname && parsed.hostname !== "localhost") throw new Error("file URL authority must be empty or localhost")
  return decodeURIComponent(parsed.pathname)
}
function strictBase64(value: string): Buffer {
  if (!value || value.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value) || (value.includes("=") && !/=+$/.test(value))) throw new Error("invalid base64 data URL payload")
  const bytes = Buffer.from(value, "base64")
  if (bytes.length === 0 || bytes.toString("base64") !== value) throw new Error("invalid base64 data URL payload")
  return bytes
}
function safeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.replace(/(?:[A-Za-z]:)?\/[^\s'"`]+/g, "local source")
}
function extension(part: any, mime: string): string { return extname(safeName(part?.filename)) || ({ "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/pdf": ".pdf" } as Record<string, string>)[mime] || ".bin" }
function writeAll(fd: number, bytes: Buffer): void {
  let offset = 0
  while (offset < bytes.length) offset += writeSync(fd, bytes, offset, bytes.length - offset)
}


async function stageBytes(bytes: Buffer, filename: string, mime: string, dir: string, max: number, remaining: number): Promise<{ path: string; size: number; sha256: string; source: string }> {
  if (bytes.length > max) throw new Error(`materialized file exceeds maxMaterializedBytes (${max})`)
  if (bytes.length > remaining) throw new Error("transform exceeds maxTotalMaterializedBytes")
  const hash = createHash("sha256").update(bytes).digest("hex")
  const target = join(privateDir(dir), `${hash}${extension({ filename }, mime).toLowerCase()}`)
  const existing = existsSync(target) ? lstatSync(target) : null
  if (existing && (!existing.isFile() || existing.isSymbolicLink())) throw new Error("materialization target is not a regular file")
  if (existing && statSync(target).size !== bytes.length) throw new Error("materialization target size mismatch")
  if (!existing) {
    const temp = `${target}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`
    const fd = openSync(temp, "wx", 0o600)
    try { writeAll(fd, bytes); closeSync(fd); renameSync(temp, target) } catch (error) { try { closeSync(fd) } catch {}; try { unlinkSync(temp) } catch {}; throw error }
  }
  if ((statSync(target).mode & 0o777) !== 0o600) chmodSync(target, 0o600)
  return { path: resolve(target), size: bytes.length, sha256: hash, source: "data-url" }
}
async function stageFile(input: string, filename: string, mime: string, dir: string, max: number, remaining: number): Promise<{ path: string; size: number; sha256: string; source: string }> {
  const source = resolve(input); const link = lstatSync(source); if (link.isSymbolicLink()) throw new Error("source symlinks are not accepted")
  const st = statSync(source)
  if (!st.isFile()) throw new Error("source is not a regular file")
  try { accessSync(source, fsConstants.R_OK) } catch { throw new Error("source is not readable") }
  if (st.size > max) throw new Error(`materialized file exceeds maxMaterializedBytes (${max})`)
  if (st.size > remaining) throw new Error("transform exceeds maxTotalMaterializedBytes")
  const hash = createHash("sha256")
  const temp = join(privateDir(dir), `.staging-${process.pid}-${Math.random().toString(16).slice(2)}`)
  const fd = openSync(temp, "wx", 0o600)
  let size = 0
  try {
    for await (const chunk of createReadStream(source)) {
      size += chunk.length
      if (size > max) throw new Error(`materialized file exceeds maxMaterializedBytes (${max})`)
      if (size > remaining) throw new Error("transform exceeds maxTotalMaterializedBytes")
      hash.update(chunk); writeAll(fd, chunk)
    }
    closeSync(fd)
    const digest = hash.digest("hex"); const target = join(privateDir(dir), `${digest}${extension({ filename }, mime).toLowerCase()}`)
    const existing = existsSync(target) ? lstatSync(target) : null
    if (existing && (!existing.isFile() || existing.isSymbolicLink())) throw new Error("materialization target is not a regular file")
    if (existing && statSync(target).size !== size) throw new Error("materialization target size mismatch")
    if (!existing) renameSync(temp, target)
    else unlinkSync(temp)
    if ((statSync(target).mode & 0o777) !== 0o600) chmodSync(target, 0o600)
    return { path: resolve(target), size, sha256: digest, source: "local" }
  } catch (error) { try { closeSync(fd) } catch {}; try { unlinkSync(temp) } catch {}; throw new Error(safeError(error)) }
}
async function materialize(part: any, mime: string, dir: string, max: number, remaining: number): Promise<{ path: string | null; size: number | null; sha256: string | null; source: string; error?: string }> {
  const url = typeof part?.url === "string" ? part.url : ""
  if (url.startsWith("data:")) {
    const comma = url.indexOf(","); if (comma < 0) throw new Error("data URL has no payload separator")
    const meta = url.slice(5, comma); if (!/;base64(?:;|$)/i.test(meta)) throw new Error("only base64 data URLs are supported")
    const payload = url.slice(comma + 1); const padding = payload.endsWith("==") ? 2 : payload.endsWith("=") ? 1 : 0
    const estimatedSize = Math.floor(payload.length / 4) * 3 - padding
    if (estimatedSize > max) throw new Error(`materialized file exceeds maxMaterializedBytes (${max})`)
    if (estimatedSize > remaining) throw new Error("transform exceeds maxTotalMaterializedBytes")
    return await stageBytes(strictBase64(payload), safeName(part?.filename), mime, dir, max, remaining)
  }
  const source = typeof part?.source?.path === "string" && part.source.path ? part.source.path : url.startsWith("file:") ? fileUrlPath(url) : url.startsWith("/") ? url : ""
  if (source) return await stageFile(source, safeName(part?.filename), mime, dir, max, remaining)
  return { path: null, size: null, sha256: null, source: /^https?:\/\//i.test(url) ? "remote" : "unresolved", error: url ? "remote attachment was not downloaded" : "no local source" }
}
function identity(part: any): Record<string, unknown> { return Object.fromEntries(["id", "sessionID", "messageID"].filter(k => part?.[k] !== undefined).map(k => [k, part[k]])) }
function isMatching(part: any, patterns: string[]): boolean { return part?.type === "file" && typeof part.mime === "string" && matchesMime(part.mime, patterns) }
function errorPart(part: any, message: string): any {
  const mime = typeof part?.mime === "string" ? part.mime : "application/octet-stream"
  const record = { schema_version: 1, filename: safeName(part?.filename), path: null, mime, media_kind: mediaKind(mime), size: null, sha256: null, source: "error", error: safeError(message) }
  return { ...identity(part), type: "text", text: `[media-guard attachment manifest]\n${JSON.stringify(record)}`, synthetic: true }
}

export const MediaGuardPlugin: Plugin = async (_context, opts: Options = {}) => {
  const configured = limits(opts); const patterns = opts.mimes ?? DEFAULT_MIMES; const dir = privateDir(opts.materializationDir ?? DEFAULT_DIR)
  return { "experimental.chat.messages.transform": async (_input: any, output: any) => {
    const snapshots = new Map<any, any[]>(); const messages = Array.isArray(output?.messages) ? output.messages : []
    try {
      for (const message of messages) if (Array.isArray(message?.parts)) snapshots.set(message, message.parts.slice())
      const fileMatches = messages.flatMap((m: any) => Array.isArray(m?.parts) ? m.parts.filter((p: any) => p?.type === "file" && typeof p.mime === "string" && matchesMime(p.mime, patterns)) : [])
      if (fileMatches.length > configured.maxFilesPerTransform) throw new Error(`transform has more than maxFilesPerTransform (${configured.maxFilesPerTransform}) files`)
      let total = 0
      for (const message of messages) {
        if (!Array.isArray(message?.parts)) continue; snapshots.set(message, message.parts.slice())
        const transformed: any[] = []
        for (const part of message.parts) {
          if (!isMatching(part, patterns)) { transformed.push(part); continue }
          try {
            const staged = await materialize(part, part.mime, dir, configured.maxMaterializedBytes, configured.maxTotalMaterializedBytes - total)
            total += staged.size ?? 0
            const record = { schema_version: 1, filename: safeName(part.filename), path: staged.path, mime: part.mime, media_kind: mediaKind(part.mime), size: staged.size, sha256: staged.sha256, source: staged.source, ...(staged.error ? { error: staged.error } : {}) }
            transformed.push({ ...identity(part), type: "text", text: `[media-guard attachment manifest]\n${JSON.stringify(record)}`, synthetic: true })
          } catch (error) {
            transformed.push(errorPart(part, error))
          }
        }
        message.parts = transformed
      }
    } catch (error) {
      console.error("[media-guard] transform failed:", safeError(error))
      for (const [message, parts] of snapshots) message.parts = parts.map(part => isMatching(part, patterns) ? errorPart(part, error) : part)
    }
  } }
}
