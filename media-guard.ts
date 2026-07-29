// Materialize attachments. Never inspect media content beyond staging/hash bytes.
import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import { accessSync, appendFileSync, chmodSync, createReadStream, existsSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, statSync, closeSync, unlinkSync, writeSync, writeFileSync, constants as fsConstants, rmSync, readdirSync } from "node:fs"
import { basename, dirname, extname, join, resolve, relative } from "node:path"
import { fileURLToPath } from "node:url"
import { tmpdir } from "node:os"

type Limits = { maxMaterializedBytes: number; maxFilesPerTransform: number; maxTotalMaterializedBytes: number }
type Options = Partial<Limits & PreprocessSettings> & { mimes?: string[]; materializationDir?: string; cacheDir?: string; extractors?: Partial<Record<Kind, Extractor>> }
const DEFAULT_MIMES = ["image/*", "application/pdf", "audio/*", "video/*", "application/zip", "application/x-zip-compressed", "application/gzip", "application/x-gzip", "application/x-tar", "application/x-bzip2", "application/x-xz", "application/x-7z-compressed", "application/vnd.rar", "application/x-rar-compressed"]
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
function canonicalMime(mime: string): string {
  return mime.split(";", 1)[0].trim().toLowerCase()
}
function matchesMime(mime: string, patterns: string[]): boolean {
  const normalized = canonicalMime(mime)
  return patterns.some(pattern => { const p = canonicalMime(pattern); return p === normalized || (p.endsWith("/*") && normalized.startsWith(p.slice(0, -1))) })
}
function mediaKind(mime: string): string { const normalized = canonicalMime(mime); return normalized === "application/pdf" ? "pdf" : normalized.startsWith("image/") ? "image" : normalized.startsWith("audio/") ? "audio" : normalized.startsWith("video/") ? "video" : ["application/zip", "application/x-zip-compressed", "application/gzip", "application/x-gzip", "application/x-tar", "application/x-bzip2", "application/x-xz", "application/x-7z-compressed", "application/vnd.rar", "application/x-rar-compressed"].includes(normalized) ? "archive" : "file" }
function safeName(value: unknown): string {
  const name = basename(typeof value === "string" ? value : "attachment").replace(/[\u0000-\u001f\u007f/\\]/g, "_").replace(/[^A-Za-z0-9._ -]/g, "_").trim()
  return name && name !== "." && name !== ".." ? name : "attachment"
}
function privateDir(dir: string, kind = "materialization"): string {
  const resolved = resolve(dir)
  if (existsSync(resolved) && lstatSync(resolved).isSymbolicLink()) throw new Error(`${kind} directory symlinks are not accepted`)
  mkdirSync(resolved, { recursive: true, mode: 0o700 })
  if (!lstatSync(resolved).isDirectory()) throw new Error("materialization path is not a directory")
  chmodSync(resolved, 0o700)
  return realpathSync(resolved)
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
function diagnosticLog(dir: string, hook: string, parts: any[], error?: unknown): void {
  try {
    const record = {
      timestamp: new Date().toISOString(),
      hook,
      partCount: parts.length,
      parts: parts.map(part => ({ type: part?.type, mime: part?.mime })),
      ...(error ? { error: { message: safeError(error), stack: error instanceof Error ? error.stack : String(error) } } : {}),
    }
    appendFileSync(join(dir, "media-guard.log"), `${JSON.stringify(record)}\n`, { mode: 0o600 })
  } catch {}
}
function extension(part: any, mime: string): string { return extname(safeName(part?.filename)) || ({ "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/pdf": ".pdf", "application/zip": ".zip", "application/x-zip-compressed": ".zip", "application/gzip": ".tar.gz", "application/x-gzip": ".gz", "application/x-tar": ".tar", "application/x-bzip2": ".bz2", "application/x-xz": ".xz", "application/x-7z-compressed": ".7z", "application/vnd.rar": ".rar", "application/x-rar-compressed": ".rar" } as Record<string, string>)[canonicalMime(mime)] || ".bin" }
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
  const mime = typeof part?.mime === "string" ? canonicalMime(part.mime) : "application/octet-stream"
  const record = { schema_version: 1, filename: safeName(part?.filename), path: null, mime, media_kind: mediaKind(mime), size: null, sha256: null, source: "error", error: safeError(message) }
  return { ...identity(part), type: "text", text: `[media-guard attachment manifest]\n${JSON.stringify(record)}`, synthetic: true }
}

type Kind = "pdf" | "image" | "audio" | "video" | "text" | "archive" | "other"
type PreprocessSettings = { maxExtractedChars: number; timeoutMs: number; maxFilesPerTransform: number; enabledKinds: string[]; maxArchiveEntries: number; maxArchiveBytes: number; maxCompressionRatio: number; maxPdfPageImages: number; maxVideoKeyframes: number }
type Extractor = (path: string, timeoutMs: number) => Promise<string>
export type MediaPreprocessOptions = Partial<PreprocessSettings> & { cacheDir?: string; extractors?: Partial<Record<Kind, Extractor>> }

const PREPROCESS_FALLBACKS: PreprocessSettings = { maxExtractedChars: 200000, timeoutMs: 300000, maxFilesPerTransform: 16, enabledKinds: ["pdf", "image", "audio", "video", "text", "archive"], maxArchiveEntries: 200, maxArchiveBytes: 524288000, maxCompressionRatio: 200, maxPdfPageImages: 50, maxVideoKeyframes: 20 }
export const MARKERS = {
  extracted: "[media-preprocess extracted:",
  archive: "[media-preprocess archive:",
  archiveFailed: "[media-preprocess archive failed:",
  failed: "[media-preprocess failed:",
  uncertain: "[media-preprocess uncertain:",
  autoExtracted: "[media-preprocess auto-extracted:",
  needsAgent: "[media-preprocess needs-agent:",
  pdfPages: "[media-preprocess pdf-pages:",
  pdfPagesFailed: "[media-preprocess pdf-pages-failed:",
  videoKeyframes: "[media-preprocess video-keyframes:",
  videoKeyframesFailed: "[media-preprocess video-keyframes-failed:",
} as const
const MIME: Record<string, string> = { ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json", ".xml": "application/xml", ".html": "text/html", ".log": "text/plain" }

function workspaceConfig(): Partial<PreprocessSettings> {
  try {
    const text = readFileSync(CONFIG_PATH, "utf8")
    const section = text.match(/^media_preprocess:\s*\n((?:^[ \t]+[^\n]*\n?)+)/m)?.[1] ?? ""
    const out: Partial<PreprocessSettings> = {}
    for (const key of ["maxExtractedChars", "timeoutMs", "maxFilesPerTransform", "maxArchiveEntries", "maxArchiveBytes", "maxCompressionRatio", "maxPdfPageImages", "maxVideoKeyframes"] as const) {
      const m = section.match(new RegExp(`^\\s*${key}:\\s*(\\d+)\\s*$`, "m")); if (m) out[key] = Number(m[1])
    }
    const inline = (key: string): string[] | undefined => { const m = section.match(new RegExp(`^\\s*${key}:\\s*\\[([^\\]]*)\\]`, "m")); return m ? m[1].split(",").map(v => v.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean) : undefined }
    const enabled = inline("enabledKinds"); if (enabled) out.enabledKinds = enabled
    return out
  } catch { return {} }
}
function settings(opts: MediaPreprocessOptions): PreprocessSettings {
  const w = workspaceConfig()
  return {
    maxExtractedChars: positive(opts.maxExtractedChars, positive(w.maxExtractedChars, PREPROCESS_FALLBACKS.maxExtractedChars)),
    timeoutMs: positive(opts.timeoutMs, positive(w.timeoutMs, PREPROCESS_FALLBACKS.timeoutMs)),
    maxFilesPerTransform: positive(opts.maxFilesPerTransform, positive(w.maxFilesPerTransform, PREPROCESS_FALLBACKS.maxFilesPerTransform)),
    enabledKinds: Array.isArray(opts.enabledKinds) ? opts.enabledKinds : (w.enabledKinds ?? PREPROCESS_FALLBACKS.enabledKinds),
    maxArchiveEntries: positive(opts.maxArchiveEntries, positive(w.maxArchiveEntries, PREPROCESS_FALLBACKS.maxArchiveEntries)),
    maxArchiveBytes: positive(opts.maxArchiveBytes, positive(w.maxArchiveBytes, PREPROCESS_FALLBACKS.maxArchiveBytes)),
    maxCompressionRatio: positive(opts.maxCompressionRatio, positive(w.maxCompressionRatio, PREPROCESS_FALLBACKS.maxCompressionRatio)),
    maxPdfPageImages: positive(opts.maxPdfPageImages, positive(w.maxPdfPageImages, PREPROCESS_FALLBACKS.maxPdfPageImages)),
    maxVideoKeyframes: positive(opts.maxVideoKeyframes, positive(w.maxVideoKeyframes, PREPROCESS_FALLBACKS.maxVideoKeyframes)),
  }
}

function sh(value: string): string { return `'${value.replace(/'/g, `'"'"'`)}'` }
function run(command: string, timeoutMs: number): Promise<string> {
  const out = join(realpathSync(tmpdir()), `opencode-media-preprocess-${process.pid}-${Math.random().toString(16).slice(2)}.out`)
  return new Promise((ok, fail) => {
    // `exec` makes timeout kill the extractor itself rather than only its shell.
    const child = Bun.spawn(["sh", "-c", `exec ${command} > ${sh(out)}`], { stderr: "ignore" })
    let timed = false
    const timer = setTimeout(() => { timed = true; child.kill(); try { unlinkSync(out) } catch {}; fail(new Error(`extractor timed out after ${timeoutMs}ms`)) }, timeoutMs)
    child.exited.then(code => {
      clearTimeout(timer); if (timed) return
      try { const text = existsSync(out) ? readFileSync(out, "utf8") : ""; unlinkSync(out); if (code !== 0) fail(new Error(`extractor exited with status ${code}`)); else ok(text) } catch (e) { fail(e) }
    }).catch(e => { clearTimeout(timer); fail(e) })
  })
}
const PYTHON = "/Users/alanshum/.claude/skills/tool--pdf/scripts/.venv/bin/python"
const PDF_RENDER_SCRIPT = "/Users/alanshum/.claude/skills/tool--pdf/scripts/pdf_render_pages.py"
const PDF_LONG_EDGE_PX = 1568
const VIDEO_KEYFRAME_SCRIPT = "/Users/alanshum/.claude/skills/tool--transcribe/scripts/video_keyframes.py"
const VIDEO_LONG_EDGE_PX = 1568

const defaults: Record<Kind, Extractor> = {
  pdf: async (path, timeout) => {
    const py = "/Users/alanshum/.claude/skills/tool--pdf/scripts/.venv/bin/python", script = "/Users/alanshum/.claude/skills/tool--pdf/scripts/pdf_tool.py"
    const raw = await run(`${sh(py)} ${sh(script)} read-text ${sh(path)} --format json`, timeout)
    let text = ""
    try {
      const json = JSON.parse(raw)
      const collect = (value: any): void => { if (typeof value === "string") text += `${value}\n`; else if (Array.isArray(value)) value.forEach(collect); else if (value && typeof value === "object") Object.entries(value).forEach(([key, item]) => { if (key.toLowerCase() === "text") collect(item) }) }
      collect(json)
    } catch { text = raw }
    if (text.trim()) return text
    const ocr = "/Users/alanshum/.claude/skills/tool--pdf/scripts/ocr_extract.py"
    const ocrRaw = await run(`${sh(py)} ${sh(ocr)} ${sh(path)}`, timeout)
    return ocrRaw.trim()
  },
  image: async (path, timeout) => { const raw = await run(`${sh("/Users/alanshum/.claude/skills/image--apple-vision-ocr/scripts/apple-vision-ocr")} ${sh(path)}`, timeout); const j = JSON.parse(raw); if (j.status !== "ok" || j.error) throw new Error(j.error || `OCR status ${j.status}`); return j.text || "" },
  audio: async (path, timeout) => transcribe(path, timeout),
  video: async (path, timeout) => transcribe(path, timeout),
}
function classify(path: string, mime = ""): Kind {
  const m = mime.split(";", 1)[0].toLowerCase(), e = extname(path).toLowerCase()
  if (m === "application/pdf" || e === ".pdf") return "pdf"
  if (m.startsWith("image/") || [".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"].includes(e)) return "image"
  if (m.startsWith("audio/") || [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"].includes(e)) return "audio"
  if (m.startsWith("video/") || [".mp4", ".mov", ".mkv", ".webm", ".avi"].includes(e)) return "video"
  if (m.includes("zip") || m.includes("gzip") || m.includes("tar") || m.includes("bzip") || m.includes("xz") || m.includes("7z") || m.includes("rar") || [".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar"].includes(e)) return "archive"
  if (m.startsWith("text/") || [".txt", ".md", ".csv", ".json", ".xml", ".html", ".log", ".yaml", ".yml", ".ts", ".js", ".css"].includes(e)) return "text"
  return "other"
}
function archiveCommand(path: string, out: string, mime: string): { list: string; sizes: string; extract: string } {
  const m = mime.toLowerCase(), p = path.toLowerCase()
  if (m.includes("zip") || p.endsWith(".zip")) return { list: `unzip -Z1 ${sh(path)}`, sizes: `unzip -Zt ${sh(path)}`, extract: `unzip -o ${sh(path)} -d ${sh(out)}` }
  if (m.includes("7z") || p.endsWith(".7z")) return { list: `7z l -slt ${sh(path)}`, sizes: `7z l -slt ${sh(path)}`, extract: `7z x -y ${sh(path)} -o${sh(out)}` }
  if (m.includes("rar") || p.endsWith(".rar")) return { list: `7z l -slt ${sh(path)}`, sizes: `7z l -slt ${sh(path)}`, extract: `7z x -y ${sh(path)} -o${sh(out)}` }
  return { list: `tar -tf ${sh(path)}`, sizes: `tar -tvf ${sh(path)}`, extract: `tar -xf ${sh(path)} -C ${sh(out)}` }
}
function safeMember(name: string): boolean { return name.length > 0 && !name.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(name) && !name.split(/[\\/]+/).includes("..") }
function filesUnder(root: string): { files: string[]; skipped: number } {
  const result: string[] = []; let skipped = 0
  const walk = (dir: string) => { for (const name of readdirSync(dir)) { const p = join(dir, name); const st = lstatSync(p); if (st.isSymbolicLink()) { rmSync(p, { force: true }); skipped++; continue } if (st.isDirectory()) { chmodSync(p, 0o700); walk(p) } else if (st.isFile()) result.push(p); else { rmSync(p, { recursive: true, force: true }); skipped++ } } }
  walk(root); return { files: result, skipped }
}
async function expandArchive(path: string, mime: string, cfg: PreprocessSettings, cache: string): Promise<{ files: string[]; skipped: number; truncated: boolean; root: string }> {
  const digest = createHash("sha256").update(readFileSync(path)).digest("hex"), root = privateDir(join(cache, `${digest}.archive`)), done = join(root, ".complete")
  let skipped = 0
  if (!existsSync(done)) {
    rmSync(root, { recursive: true, force: true }); privateDir(root)
    const command = archiveCommand(path, root, mime)
    let listing = ""
    try { listing = await run(command.list, cfg.timeoutMs) } catch { throw new Error("archive tool is unavailable or archive listing failed") }
    const names = listing.split("\n").map(x => x.trim()).filter(Boolean)
    if (names.length > cfg.maxArchiveEntries) throw new Error(`archive exceeds maxArchiveEntries (${cfg.maxArchiveEntries})`)
    if (names.some(x => !safeMember(x))) throw new Error("archive contains an unsafe member path")
    let declared = 0
    try {
      const sizeListing = await run(command.sizes, cfg.timeoutMs)
      if (command.sizes.includes("unzip -Zt")) {
        const match = sizeListing.match(/([0-9]+)\s+bytes\s+uncompressed/i); if (!match) throw new Error("missing declared archive size"); declared = Number(match[1])
      } else if (command.sizes.includes("tar -tvf")) {
        for (const line of sizeListing.split("\n")) { const match = line.match(/^[-dlcbps][^\s]*\s+\d+\s+\S+\s+\S+\s+(\d+)\s/); if (match) declared += Number(match[1]) }
      } else {
        for (const match of sizeListing.matchAll(/^Size = (\d+)$/gm)) declared += Number(match[1])
      }
    } catch { throw new Error("archive declared-size listing failed") }
    const archiveBytes = statSync(path).size
    if (declared > cfg.maxArchiveBytes) throw new Error(`archive exceeds maxArchiveBytes (${cfg.maxArchiveBytes})`)
    if (archiveBytes > 0 && declared / archiveBytes > cfg.maxCompressionRatio) throw new Error(`archive exceeds maxCompressionRatio (${cfg.maxCompressionRatio})`)
    try { await run(command.extract, cfg.timeoutMs) } catch { throw new Error("archive extraction failed") }
    const base = realpathSync(root), found = filesUnder(root), files = found.files
    skipped += found.skipped
    let total = 0
    for (const file of files) { let real: string; try { real = realpathSync(file) } catch { rmSync(file, { force: true }); skipped++; continue } const rel = relative(base, real); if (rel.startsWith("..") || resolve(base, rel) !== real || !safeMember(rel)) { rmSync(file, { force: true }); skipped++; continue } const st = lstatSync(file); total += st.size; if (total > cfg.maxArchiveBytes) { rmSync(root, { recursive: true, force: true }); throw new Error(`archive exceeds maxArchiveBytes (${cfg.maxArchiveBytes})`) } chmodSync(file, 0o600) }
    writeFileSync(done, "ok", { mode: 0o600 }); chmodSync(done, 0o600)
  }
  const found = filesUnder(root), files = found.files.filter(p => p !== done), count = files.length
  if (count > cfg.maxArchiveEntries) throw new Error(`archive exceeds maxArchiveEntries (${cfg.maxArchiveEntries})`)
  const bytes = files.reduce((total, file) => total + statSync(file).size, 0)
  if (bytes > cfg.maxArchiveBytes) throw new Error(`archive exceeds maxArchiveBytes (${cfg.maxArchiveBytes})`)
  return { files, skipped: skipped + found.skipped, truncated: false, root }
}
async function transcribe(path: string, timeout: number): Promise<string> {
  const py = "/Users/alanshum/.claude/skills/tool--transcribe/scripts/.venv/bin/python", script = "/Users/alanshum/.claude/skills/tool--transcribe/scripts/transcribe_audio.py"
  const dir = privateDir(join(realpathSync(tmpdir()), `opencode-media-preprocess-transcript-${process.pid}-${Math.random().toString(16).slice(2)}`))
  try {
    await run(`${sh(py)} ${sh(script)} ${sh(path)} --backend auto --model turbo --formats txt --output-dir ${sh(dir)}`, timeout)
    const files = Bun.file(join(dir, `${path.split("/").pop()!.replace(/\.[^.]*$/, "")}.txt`))
    if (!(await files.exists())) throw new Error("transcriber did not write a txt output")
    return await files.text()
  } finally { rmSync(dir, { recursive: true, force: true }) }
}
function parseManifest(part: any): any | null {
  if (part?.type !== "text" || typeof part.text !== "string" || !part.text.startsWith("[media-guard attachment manifest]")) return null
  try { return JSON.parse(part.text.split("\n", 2)[1]) } catch { return null }
}
function localPath(path: unknown): asserts path is string { if (typeof path !== "string" || !path.startsWith("/") || /^[a-z][a-z0-9+.-]*:/i.test(path)) throw new Error("manifest path is not a local absolute path") }
function extractorName(kind: Kind) { return kind === "pdf" ? "pdf_tool.read-text" : kind === "image" ? "apple-vision-ocr" : "transcribe_audio" }
async function extractOne(kind: Kind, path: string, mime: string, cfg: PreprocessSettings, cache: string, extractors: Record<Kind, Extractor>, knownHash?: string): Promise<string> {
  const hash = knownHash ?? (existsSync(path) ? createHash("sha256").update(readFileSync(path)).digest("hex") : ""), key = hash ? join(cache, `${hash}.${kind}.txt`) : null
  if (key && existsSync(key)) { chmodSync(key, 0o600); return readFileSync(key, "utf8") }
  if (kind === "text") { const text = readFileSync(path, "utf8"); if (key) { writeFileSync(key, text, { mode: 0o600 }); chmodSync(key, 0o600) }; return text }
  if (!(kind in extractors)) return ""
  const text = await extractors[kind](path, cfg.timeoutMs)
  if (key) { writeFileSync(key, text, { mode: 0o600 }); chmodSync(key, 0o600) }
  return text
}
async function augmentArchive(part: any, manifest: any, cfg: PreprocessSettings, cache: string, extractors: Record<Kind, Extractor>): Promise<any> {
  try {
    localPath(manifest.path)
    const expanded = await expandArchive(manifest.path, manifest.mime, cfg, cache), entries: any[] = [], auto: string[] = [], needs: any[] = []
    let used = 0, truncated = false
    for (const path of expanded.files) {
      const mime = MIME[extname(path).toLowerCase()] ?? "application/octet-stream", kind = classify(path, mime), size = statSync(path).size
       const entry = { name: relative(expanded.root, path), path: resolve(path), mime, kind, size, handling: kind === "archive" ? "nested-archive-skipped" : ["pdf", "audio", "video", "text"].includes(kind) ? "auto-preprocessed" : "needs-agent" }
      entries.push(entry)
       if (entry.handling !== "auto-preprocessed") { if (entry.handling === "needs-agent") needs.push(entry); continue }
      try {
        let text = await extractOne(kind, path, mime, cfg, cache, extractors)
        if (used + text.length > cfg.maxExtractedChars) { text = text.slice(0, Math.max(0, cfg.maxExtractedChars - used)); truncated = true }
        used += text.length
        if (text.trim()) auto.push(`--- ${entry.name} ---\n${text}`)
      } catch (e) { auto.push(`--- ${entry.name} ---\n${MARKERS.failed} kind=${kind} reason=${safeError(e)}]`) }
    }
    const block = [`${MARKERS.archive} entries=${entries.length} expanded=${expanded.files.length} skipped=${expanded.skipped} truncated=${truncated}]`, JSON.stringify({ entries }), `${MARKERS.autoExtracted} ${auto.length} entries]`, ...auto, `${MARKERS.needsAgent} ${needs.length} entries]`]
    if (needs.length) block.push("Dispatch `vision-reader` via `task` against these local paths before answering questions about them:", ...needs.map(e => `- ${e.path} (${e.mime}; kind=${e.kind})`))
    else block.push("No members require agent handling; the auto-extracted bucket is complete.")
    return { ...part, text: `${part.text}\n${block.join("\n")}` }
  } catch (e) { return { ...part, text: `${part.text}\n${MARKERS.archiveFailed} reason=${safeError(e)}]` } }
}
function augment(part: any, cfg: PreprocessSettings, cache: string, extractors: Record<Kind, Extractor>): Promise<any> {
  const manifest = parseManifest(part); if (!manifest || !manifest.path || Object.prototype.hasOwnProperty.call(manifest, "error") || processed(part.text)) return Promise.resolve(part)
  const kind = classify(manifest.path, manifest.mime) === "other" ? manifest.media_kind as Kind : classify(manifest.path, manifest.mime)
  if (!cfg.enabledKinds.includes(kind)) return Promise.resolve(part)
  if (kind === "archive") return augmentArchive(part, manifest, cfg, cache, extractors)
  if (!["pdf", "image", "audio", "video"].includes(kind)) return Promise.resolve(part)
  return (async () => {
    try {
      localPath(manifest.path); const text = await extractOne(kind, manifest.path, manifest.mime, cfg, cache, extractors, typeof manifest.sha256 === "string" && /^[a-f0-9]{64}$/i.test(manifest.sha256) ? manifest.sha256 : undefined); const clipped = text.slice(0, cfg.maxExtractedChars), truncated = clipped.length < text.length; const label = clipped.trim() ? `${MARKERS.extracted} kind=${kind} extractor=${extractorName(kind)} chars=${clipped.length} truncated=${truncated}]` : `${MARKERS.uncertain} kind=${kind} extractor=${extractorName(kind)} reason=empty output]`
      let result = `${part.text}
${label}${clipped.trim() ? `
${clipped}` : ""}`
      if (kind === "pdf" && existsSync(manifest.path)) {
        try {
          const sha256 = typeof manifest.sha256 === "string" && /^[a-f0-9]{64}$/i.test(manifest.sha256) ? manifest.sha256 : undefined
          const pages = await extractPdfPages(manifest.path, sha256, cfg, cache)
          result += `
${MARKERS.pdfPages} count=${pages.count} maxPages=${cfg.maxPdfPageImages} truncated=${pages.truncated}]`
          for (const p of pages.paths) result += `
${p}`
          if (pages.count > 0) {
            result += `
Dispatch \`vision-reader\` via \`task\` against these local paths before answering questions about them:`
            for (let i = 0; i < pages.paths.length; i++) result += `
- ${pages.paths[i]} (image/webp; page ${i + 1})`
          }
        } catch (pageError) {
          result += `
${MARKERS.pdfPagesFailed} failed=${safeError(pageError)}]`
        }
      }
      if (kind === "video" && existsSync(manifest.path)) {
        try {
          const sha256 = typeof manifest.sha256 === "string" && /^[a-f0-9]{64}$/i.test(manifest.sha256) ? manifest.sha256 : undefined
          const keyframes = await extractVideoKeyframes(manifest.path, sha256, cfg, cache)
          result += `
${MARKERS.videoKeyframes} count=${keyframes.count} maxFrames=${cfg.maxVideoKeyframes} truncated=${keyframes.truncated}]`
          for (const k of keyframes.paths) result += `
${k}`
          if (keyframes.count > 0) {
            result += `
Dispatch \`vision-reader\` via \`task\` against these local paths before answering questions about them:`
            for (let i = 0; i < keyframes.paths.length; i++) result += `
- ${keyframes.paths[i]} (image/webp; frame ${i + 1} @ ${keyframes.timestamps[i]?.toFixed(3) ?? "?"}s)`
          }
        } catch (keyframeError) {
          result += `
${MARKERS.videoKeyframesFailed} failed=${safeError(keyframeError)}]`
        }
      }
      return { ...part, text: result }
    } catch (e) { return { ...part, text: `${part.text}
${MARKERS.failed} kind=${kind} reason=${safeError(e)}]` } }
  })()
}
function processed(text: string): boolean { return Object.values(MARKERS).some(marker => text.includes(marker)) }

interface PdfPagesResult { paths: string[]; count: number; truncated: boolean }
interface VideoKeyframesResult { paths: string[]; timestamps: number[]; count: number; truncated: boolean }
async function extractPdfPages(path: string, knownHash: string | undefined, cfg: PreprocessSettings, cache: string): Promise<PdfPagesResult> {
  const hash = knownHash ?? (existsSync(path) ? createHash("sha256").update(readFileSync(path)).digest("hex") : "")
  if (!hash) throw new Error("cannot compute pdf hash")
  const pageDir = join(cache, `${hash}.pdfpages`)
  if (!existsSync(pageDir)) privateDir(pageDir, "cache")
  const completeMarker = join(pageDir, ".complete")
  if (existsSync(completeMarker)) {
    const meta = readFileSync(completeMarker, "utf8").trim().split("\n")
    const rendered = Number(meta[0]) || 0
    const total = Number(meta[1]) || rendered
    const pagePaths = readdirSync(pageDir).filter(f => f.endsWith(".webp")).sort().map(f => { const p = resolve(join(pageDir, f)); chmodSync(p, 0o600); return p })
    return { paths: pagePaths, count: rendered, truncated: total > cfg.maxPdfPageImages }
  }
  await run(`${sh(PYTHON)} ${sh(PDF_RENDER_SCRIPT)} ${sh(path)} ${sh(pageDir)} --max-pages ${cfg.maxPdfPageImages} --long-edge ${PDF_LONG_EDGE_PX}`, cfg.timeoutMs)
  if (!existsSync(completeMarker)) throw new Error("pdf page renderer did not write completion marker")
  const meta = readFileSync(completeMarker, "utf8").trim().split("\n")
  const rendered = Number(meta[0]) || 0
  const total = Number(meta[1]) || rendered
  const pagePaths = readdirSync(pageDir).filter(f => f.endsWith(".webp")).sort().map(f => { const p = resolve(join(pageDir, f)); chmodSync(p, 0o600); return p })
  return { paths: pagePaths, count: rendered, truncated: total > cfg.maxPdfPageImages }
}
async function extractVideoKeyframes(path: string, knownHash: string | undefined, cfg: PreprocessSettings, cache: string): Promise<VideoKeyframesResult> {
  const hash = knownHash ?? (existsSync(path) ? createHash("sha256").update(readFileSync(path)).digest("hex") : "")
  if (!hash) throw new Error("cannot compute video hash")
  const frameDir = join(cache, `${hash}.keyframes`)
  if (!existsSync(frameDir)) privateDir(frameDir, "cache")
  const completeMarker = join(frameDir, ".complete")
  if (existsSync(completeMarker)) {
    const meta = readFileSync(completeMarker, "utf8").trim().split("\n")
    const extracted = Number(meta[0]) || 0
    const total = Number(meta[1]) || extracted
    const framePaths = readdirSync(frameDir).filter(f => f.endsWith(".webp")).sort().map(f => { const p = resolve(join(frameDir, f)); chmodSync(p, 0o600); return p })
    // Read timestamps from a timestamp file if it exists
    const timestamps: number[] = []
    const timestampFile = join(frameDir, ".timestamps")
    if (existsSync(timestampFile)) {
      const tsData = readFileSync(timestampFile, "utf8").trim()
      try {
        const parsed = JSON.parse(tsData)
        if (Array.isArray(parsed)) timestamps.push(...parsed)
      } catch {}
    }
    return { paths: framePaths, timestamps, count: extracted, truncated: total > cfg.maxVideoKeyframes }
  }
  const rawOutput = await run(`${sh(PYTHON)} ${sh(VIDEO_KEYFRAME_SCRIPT)} ${sh(path)} ${sh(frameDir)} --max-frames ${cfg.maxVideoKeyframes}`, cfg.timeoutMs)
  if (!existsSync(completeMarker)) throw new Error("video keyframe extractor did not write completion marker")
  const meta = readFileSync(completeMarker, "utf8").trim().split("\n")
  const extracted = Number(meta[0]) || 0
  const total = Number(meta[1]) || extracted
  const framePaths = readdirSync(frameDir).filter(f => f.endsWith(".webp")).sort().map(f => { const p = resolve(join(frameDir, f)); chmodSync(p, 0o600); return p })
  // Read timestamps from the JSON output
  let timestamps: number[] = []
  try {
    const output = JSON.parse(rawOutput)
    if (output.frames && Array.isArray(output.frames)) {
      timestamps = output.frames.map((f: any) => f.timestamp_seconds)
    }
  } catch {}
  // Write timestamps to file for cache
  writeFileSync(join(frameDir, ".timestamps"), JSON.stringify(timestamps), { mode: 0o600 })
  return { paths: framePaths, timestamps, count: extracted, truncated: total > cfg.maxVideoKeyframes }
}


export const MediaGuardPlugin: Plugin = async (_context, opts: Options = {}) => {
  const configured = limits(opts); const patterns = opts.mimes ?? DEFAULT_MIMES; const dir = privateDir(opts.materializationDir ?? DEFAULT_DIR)
  const preprocessConfig = settings(opts)
  const preprocessCache = privateDir(opts.cacheDir ?? join(realpathSync(tmpdir()), "opencode-media-preprocess"), "cache")
  const preprocessExtractors = { ...defaults, ...(opts.extractors ?? {}) } as Record<Kind, Extractor>
  const preprocessParts = async (parts: any[], state = { count: 0 }): Promise<void> => {
    const snapshot = parts.slice()
    try {
      const result: any[] = []
      for (const part of snapshot) {
        const manifest = parseManifest(part)
        const eligible = !!manifest && !!manifest.path && !Object.prototype.hasOwnProperty.call(manifest, "error") && preprocessConfig.enabledKinds.includes(manifest.media_kind) && !processed(part.text)
        if (eligible && state.count++ >= preprocessConfig.maxFilesPerTransform) result.push({ ...part, text: `${part.text}\n${MARKERS.failed} kind=${manifest.media_kind} reason=maxFilesPerTransform]` })
        else result.push(await augment(part, preprocessConfig, preprocessCache, preprocessExtractors))
      }
      parts.splice(0, parts.length, ...result)
    } catch (error) {
      console.error("[media-preprocess] transform failed:", safeError(error))
      parts.splice(0, parts.length, ...snapshot)
    }
  }
  const replaceMatches = (parts: any[], message: string): void => {
    const snapshot = parts.slice()
    parts.splice(0, parts.length, ...snapshot.map(part => isMatching(part, patterns) ? errorPart(part, message) : part))
  }
  const transformParts = async (parts: any[], state = { files: 0, total: 0 }): Promise<void> => {
    const snapshot = parts.slice()
    const matches = snapshot.filter(part => isMatching(part, patterns))
    if (state.files + matches.length > configured.maxFilesPerTransform) {
      const message = `transform has more than maxFilesPerTransform (${configured.maxFilesPerTransform}) files`
      replaceMatches(parts, message)
      return
    }
    state.files += matches.length
    const transformed: any[] = []
    for (const part of snapshot) {
      if (!isMatching(part, patterns)) { transformed.push(part); continue }
      try {
        const mime = canonicalMime(part.mime)
        const staged = await materialize(part, mime, dir, configured.maxMaterializedBytes, configured.maxTotalMaterializedBytes - state.total)
        state.total += staged.size ?? 0
        const record = { schema_version: 1, filename: safeName(part.filename), path: staged.path, mime, media_kind: mediaKind(mime), size: staged.size, sha256: staged.sha256, source: staged.source, ...(staged.error ? { error: staged.error } : {}) }
        transformed.push({ ...identity(part), type: "text", text: `[media-guard attachment manifest]\n${JSON.stringify(record)}`, synthetic: true })
      } catch (error) { diagnosticLog(dir, "transformParts.error", [part], error); transformed.push(errorPart(part, error)) }
    }
    parts.splice(0, parts.length, ...transformed)
  }
  const transformMessages = async (input: any, output: any): Promise<void> => {
    const snapshots = new Map<any, any[]>(); const messages = Array.isArray(output?.messages) ? output.messages : []
    const state = { files: 0, total: 0 }
    const preprocessState = { count: 0 }
    const receivedParts = messages.flatMap(message => Array.isArray(message?.parts) ? message.parts : [])
    diagnosticLog(dir, "experimental.chat.messages.transform", receivedParts)
    try {
      for (const message of messages) if (Array.isArray(message?.parts)) snapshots.set(message, message.parts.slice())
      const totalMatches = [...snapshots.values()].reduce((count, parts) => count + parts.filter(part => isMatching(part, patterns)).length, 0)
      if (totalMatches > configured.maxFilesPerTransform) {
        const message = `transform has more than maxFilesPerTransform (${configured.maxFilesPerTransform}) files`
        for (const [target, parts] of snapshots) target.parts = parts.map(part => isMatching(part, patterns) ? errorPart(part, message) : part)
        return
      }
      for (const message of messages) {
        if (!Array.isArray(message?.parts)) continue; snapshots.set(message, message.parts.slice())
        await transformParts(message.parts, state)
        await preprocessParts(message.parts, preprocessState)
      }
    } catch (error) {
      diagnosticLog(dir, "experimental.chat.messages.transform.error", receivedParts, error)
      console.error("[media-guard] transform failed:", safeError(error))
      for (const [message, parts] of snapshots) message.parts = parts.map(part => isMatching(part, patterns) ? errorPart(part, error) : part)
    }
  }
  const transformChatMessage = async (_input: any, output: any): Promise<void> => {
    const parts = Array.isArray(output?.parts) ? output.parts : null
    diagnosticLog(dir, "chat.message", parts ?? [])
    if (!parts) return
    const snapshot = parts.slice()
    try { await transformParts(parts); await preprocessParts(parts) }
    catch (error) {
      diagnosticLog(dir, "chat.message.error", parts, error)
      console.error("[media-guard] chat.message transform failed:", safeError(error))
      parts.splice(0, parts.length, ...snapshot.map(part => isMatching(part, patterns) ? errorPart(part, error) : part))
    }
  }
  return { "chat.message": transformChatMessage, "experimental.chat.messages.transform": async (input: any, output: any) => transformMessages(input, output) }
}

export default MediaGuardPlugin
