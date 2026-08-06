#!/usr/bin/env bun
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, realpathSync, lstatSync, readdirSync, statSync } from "node:fs"
import { createHash } from "node:crypto"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { MARKERS, MediaGuardPlugin } from "../media-guard.ts"

const root = mkdtempSync(join(tmpdir(), "media-preprocess-test-"))
const cache = join(root, "cache")
mkdirSync(cache, { recursive: true })
const sha = "a".repeat(64)
const manifest = (kind = "pdf", path: string | null = "/private/tmp/materialized.pdf", extra = {}) => ({
  schema_version: 1, filename: "materialized.pdf", path, mime: "application/pdf", media_kind: kind,
  size: 10, sha256: sha, source: "local", ...extra,
})
const part = (record: any, id = "p1") => ({ id, sessionID: "s1", messageID: "m1", type: "text", synthetic: true,
  text: `[media-guard attachment manifest]\n${JSON.stringify(record)}` })
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message) }

const source = readFileSync(join(import.meta.dir, "..", "media-guard.ts"), "utf8")
const materializationParser = source.slice(source.indexOf("function workspaceLimits()"), source.indexOf("function limits("))
const preprocessingParser = source.slice(source.indexOf("function workspaceConfig()"), source.indexOf("function settings("))
const materializedKeys = [...materializationParser.matchAll(/^\s*\w+:\s*"([A-Za-z0-9]+)"/gm)].map(match => match[1])
const extractedKeys = [...preprocessingParser.matchAll(/^\s*([A-Za-z0-9]+):\s*"/gm)].map(match => match[1])
const documentedKeys = [...readFileSync(join(import.meta.dir, "..", "README.md"), "utf8").matchAll(/^\| `([^`]+)` \|/gm)].map(match => match[1])
for (const key of [...materializedKeys, ...extractedKeys]) assert(documentedKeys.includes(key), `parser config key is missing from README: ${key}`)

try {
  const hooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard"), cacheDir: cache, extractors: {
    pdf: async () => "extracted words",
    image: async () => "image words",
    audio: async () => "audio words",
    video: async () => "video words",
  } })
  assert(typeof hooks["chat.message"] === "function", "chat hook missing")
  assert(typeof hooks["experimental.chat.messages.transform"] === "function", "messages hook missing")

  const passthrough = { parts: [{ id: "text", type: "text", text: "ordinary" }] }
  await hooks["chat.message"]!({}, passthrough)
  assert(passthrough.parts[0].text === "ordinary", "ordinary text changed")

  for (const untouched of [part(manifest("pdf", null), "null"), part(manifest("pdf", "/x", { error: "materialization failed" }), "error")]) {
    const before = untouched.text
    const container = { parts: [untouched] }
    await hooks["chat.message"]!({}, container)
    assert(container.parts[0].text === before, "invalid manifest changed")
  }

  const first = part(manifest(), "keep-id")
  const ordinary = { id: "ordinary", type: "text", text: "after" }
  const output = { messages: [{ parts: [first, ordinary] }] }
  await hooks["experimental.chat.messages.transform"]!({}, output)
  const extracted = output.messages[0].parts[0]
  assert(extracted.id === "keep-id" && extracted.sessionID === "s1" && output.messages[0].parts[1] === ordinary, "identity/order changed")
  assert(extracted.text.split("\n", 2)[1] === JSON.stringify(manifest()), "manifest JSON changed")
  assert(extracted.text.includes("[media-preprocess extracted: kind=pdf"), "success marker missing")
  assert(extracted.text.endsWith("extracted words"), "extracted text missing")
  const once = extracted.text
  await hooks["chat.message"]!({}, { parts: [extracted] })
  assert(extracted.text === once, "idempotency failed")

  let calls = 0
  const cachedHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-cache"), cacheDir: join(root, "cache-hit"), extractors: { pdf: async () => { calls++; return "cached" } } })
  const cached1 = part(manifest("pdf", "/x"), "cache-1")
  await cachedHooks["chat.message"]!({}, { parts: [cached1] })
  const cached2 = part(manifest("pdf", "/x"), "cache-2")
  await cachedHooks["chat.message"]!({}, { parts: [cached2] })
  assert(calls === 1, `cache miss reran extractor (${calls})`)

  const failing = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-fail"), cacheDir: join(root, "cache-fail"), extractors: { pdf: async () => { throw new Error("bad extractor") } } })
  const failed = part(manifest("pdf", "/x"), "failed")
  const originalManifest = failed.text
  const failedContainer = { parts: [failed] }
  await failing["chat.message"]!({}, failedContainer)
  assert(failedContainer.parts[0].text.startsWith(originalManifest) && failedContainer.parts[0].text.includes("[media-preprocess failed: kind=pdf"), "failure did not preserve manifest")

  const documentHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-document"), cacheDir: join(root, "document-cache"), enabledKinds: ["document"], extractors: { document: async () => "converted markdown" } })
  const documentPart = part({ filename: "report.docx", path: "/x/report.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", media_kind: "document", sha256: "e".repeat(64) }, "document")
  const documentContainer = { parts: [documentPart] }
  await documentHooks["chat.message"]!({}, documentContainer)
  assert(documentContainer.parts[0].text.includes("[media-preprocess extracted: kind=document") && documentContainer.parts[0].text.includes("converted markdown"), "document extraction marker or text missing")

  const epubPart = part({ filename: "book.epub", path: "/x/book.epub", mime: "application/epub+zip", media_kind: "document", sha256: "f".repeat(64) }, "epub")
  const epubContainer = { parts: [epubPart] }
  await documentHooks["chat.message"]!({}, epubContainer)
  assert(epubContainer.parts[0].text.includes("[media-preprocess extracted: kind=document") && !epubContainer.parts[0].text.includes("archive"), "EPUB was classified as archive")

  const failingDocumentHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-document-fail"), cacheDir: join(root, "document-fail-cache"), enabledKinds: ["document"], extractors: { document: async () => { throw new Error("bad document extractor") } } })
  const failingDocument = part({ filename: "broken.docx", path: "/x/broken.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", media_kind: "document", sha256: "1".repeat(64) }, "document-fail")
  const failingDocumentContainer = { parts: [failingDocument] }
  await failingDocumentHooks["chat.message"]!({}, failingDocumentContainer)
  assert(failingDocumentContainer.parts[0].text.includes("[media-preprocess failed: kind=document") && failingDocumentContainer.parts[0].text.includes("bad document extractor"), "document failure marker missing")

  const limited = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-limit"), cacheDir: join(root, "cache-limit"), maxExtractedChars: 5, extractors: { pdf: async () => "123456789" } })
  const limitedPart = part({ ...manifest(), sha256: "b".repeat(64) }, "limited")
  const limitedContainer = { parts: [limitedPart] }
  await limited["chat.message"]!({}, limitedContainer)
  assert(limitedContainer.parts[0].text.includes("chars=5 truncated=true") && limitedContainer.parts[0].text.endsWith("12345"), "truncation incorrect")

  // Real local-file smoke check: generate a PNG without network access and run
  // the actual Apple Vision extractor. A blank image may legitimately produce
  // an uncertainty marker; either outcome proves the real dispatch ran.
  const local = join(root, "local.png")
  const image = Bun.spawnSync(["magick", "-size", "32x32", "xc:white", local])
  assert(image.exitCode === 0 && existsSync(local), "could not generate local PNG")
  const real = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-real"), cacheDir: join(root, "cache-real") })
  const realPart = part({ ...manifest("image", local), mime: "image/png", filename: "local.png", sha256: "c".repeat(64) }, "real")
  const realContainer = { parts: [realPart] }
  await real["chat.message"]!({}, realContainer)
  assert(realContainer.parts[0].text.includes("[media-preprocess "), "real local-file extraction did not run")

  // Archive fixture: plain text is auto-preprocessed and the image is explicitly routed.
  const archiveDir = join(root, "archive-input")
  mkdirSync(archiveDir)
  writeFileSync(join(archiveDir, "memo.txt"), "memo from archive")
  const archiveImage = join(archiveDir, "diagram.png")
  const generated = Bun.spawnSync(["magick", "-size", "8x8", "xc:white", archiveImage])
  assert(generated.exitCode === 0, "could not generate archive image")
  const archivePath = join(root, "bundle.zip")
  assert(Bun.spawnSync(["zip", "-q", "-r", archivePath, "."], { cwd: archiveDir }).exitCode === 0, "could not create zip")
  const archiveSha = createHash("sha256").update(readFileSync(archivePath)).digest("hex")
  const archiveHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-archive"), cacheDir: join(root, "archive-cache"), enabledKinds: ["pdf", "image", "audio", "video", "text", "archive"], extractors: { pdf: async () => "pdf", audio: async () => "audio", video: async () => "video" } })
  const archivePart = part({ filename: "bundle.zip", path: archivePath, mime: "application/zip", media_kind: "archive", sha256: archiveSha }, "archive")
  const archiveContainer = { parts: [archivePart] }
  await archiveHooks["chat.message"]!({}, archiveContainer)
  const archiveText = archiveContainer.parts[0].text
  assert(archiveText.includes("[media-preprocess archive: entries=2 expanded=2 skipped=0 truncated=false]"), "archive summary missing")
  assert(archiveText.includes("memo from archive") && archiveText.includes('"handling":"needs-agent"'), "archive partition missing")
  assert(archiveText.includes("Dispatch `vision-reader` via `task`") && archiveText.includes("diagram.png"), "vision routing missing")

  const documentArchiveDir = join(root, "document-archive-input")
  mkdirSync(documentArchiveDir)
  writeFileSync(join(documentArchiveDir, "report.docx"), "not a real office document")
  const documentArchivePath = join(root, "document-bundle.zip")
  assert(Bun.spawnSync(["zip", "-q", "-r", documentArchivePath, "."], { cwd: documentArchiveDir }).exitCode === 0, "could not create document zip")
  const documentArchiveHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-document-archive"), cacheDir: join(root, "document-archive-cache"), enabledKinds: ["document", "archive"], extractors: { document: async () => "archive document markdown" } })
  const documentArchivePart = part({ filename: "document-bundle.zip", path: documentArchivePath, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(documentArchivePath)).digest("hex") }, "document-archive")
  const documentArchiveContainer = { parts: [documentArchivePart] }
  await documentArchiveHooks["chat.message"]!({}, documentArchiveContainer)
  assert(documentArchiveContainer.parts[0].text.includes('"kind":"document"') && documentArchiveContainer.parts[0].text.includes('"handling":"auto-preprocessed"') && documentArchiveContainer.parts[0].text.includes("archive document markdown"), "document archive member was not auto-preprocessed")
  const archiveAgain = archiveContainer.parts[0].text
  await archiveHooks["chat.message"]!({}, archiveContainer)
  assert(archiveContainer.parts[0].text === archiveAgain, "archive idempotency failed")
  const archiveCachePart = part({ filename: "bundle.zip", path: archivePath, mime: "application/zip", media_kind: "archive", sha256: archiveSha }, "archive-cache-hit")
  const archiveCacheContainer = { parts: [archiveCachePart] }
  await archiveHooks["chat.message"]!({}, archiveCacheContainer)
  assert(archiveCacheContainer.parts[0].text.includes("[media-preprocess archive:"), `archive cache hit failed: ${archiveCacheContainer.parts[0].text}`)

  // Traversal, symlink, entry, byte, and corrupt-archive guards emit markers and never throw.
  const traversal = join(root, "traversal.zip")
  Bun.spawnSync(["python3", "-c", "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'w'); z.writestr('../escape.txt','x'); z.close()", traversal])
  const traversalPart = part({ filename: "traversal.zip", path: traversal, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(traversal)).digest("hex") })
  const traversalContainer = { parts: [traversalPart] }
  await archiveHooks["chat.message"]!({}, traversalContainer)
  assert(traversalContainer.parts[0].text.includes("archive failed") && !existsSync(join(root, "escape.txt")), `traversal was not rejected: ${traversalContainer.parts[0].text}`)
  const symlinkDir = join(root, "symlink-input"); mkdirSync(symlinkDir); writeFileSync(join(symlinkDir, "target.txt"), "target"); Bun.spawnSync(["ln", "-s", "target.txt", join(symlinkDir, "link.txt")])
  const symlinkTar = join(root, "symlink.tar"); Bun.spawnSync(["tar", "-cf", symlinkTar, "target.txt", "link.txt"], { cwd: symlinkDir })
  const symlinkPart = part({ filename: "symlink.tar", path: symlinkTar, mime: "application/x-tar", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(symlinkTar)).digest("hex") })
  const symlinkContainer = { parts: [symlinkPart] }
  await archiveHooks["chat.message"]!({}, symlinkContainer)
  assert(!symlinkContainer.parts[0].text.includes("link.txt") && symlinkContainer.parts[0].text.includes("entries=1"), `symlink member was not skipped: ${symlinkContainer.parts[0].text}`)
  const many = join(root, "many.zip"); const manyDir = join(root, "many-input"); mkdirSync(manyDir); for (let i = 0; i < 3; i++) writeFileSync(join(manyDir, `${i}.txt`), "x"); Bun.spawnSync(["zip", "-q", many, "0.txt", "1.txt", "2.txt"], { cwd: manyDir })
  const limitedArchive = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-limited-archive"), cacheDir: join(root, "limited-archive-cache"), enabledKinds: ["archive"], maxArchiveEntries: 2 })
  const manyPart = part({ filename: "many.zip", path: many, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(many)).digest("hex") })
  const manyContainer = { parts: [manyPart] }
  await limitedArchive["chat.message"]!({}, manyContainer); assert(manyContainer.parts[0].text.includes("archive failed"), "entry cap was not enforced")
  const bytesPart = part({ filename: "bundle.zip", path: archivePath, mime: "application/zip", media_kind: "archive", sha256: archiveSha })
  const byteLimited = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-byte"), cacheDir: join(root, "byte-cache"), enabledKinds: ["archive"], maxArchiveBytes: 1 })
  const bytesContainer = { parts: [bytesPart] }
  await byteLimited["chat.message"]!({}, bytesContainer); assert(bytesContainer.parts[0].text.includes("maxArchiveBytes"), "byte cap was not enforced")
  const corrupt = join(root, "corrupt.zip"); writeFileSync(corrupt, "not a zip")
  const corruptPart = part({ filename: "corrupt.zip", path: corrupt, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(corrupt)).digest("hex") })
  const corruptContainer = { parts: [corruptPart] }
  await archiveHooks["chat.message"]!({}, corruptContainer); assert(corruptContainer.parts[0].text.includes("archive failed"), "corrupt archive did not fail safely")

  // Declared-size and compression-ratio gates must run before the extractor.
  const bombInput = join(root, "bomb-input"); mkdirSync(bombInput); writeFileSync(join(bombInput, "payload.txt"), "x".repeat(1024 * 1024))
  const bomb = join(root, "bomb.zip"); assert(Bun.spawnSync(["zip", "-q", "-r", bomb, "."], { cwd: bombInput }).exitCode === 0, "could not create ratio fixture")
  const bombCache = join(root, "bomb-cache")
  const bombHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-bomb"), cacheDir: bombCache, enabledKinds: ["archive"], maxCompressionRatio: 2 })
  const bombPart = part({ filename: "bomb.zip", path: bomb, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(bomb)).digest("hex") })
  const bombContainer = { parts: [bombPart] }; await bombHooks["chat.message"]!({}, bombContainer)
  assert(bombContainer.parts[0].text.includes("maxCompressionRatio"), "ratio cap was not enforced")
  const bombRoot = join(bombCache, `${createHash("sha256").update(readFileSync(bomb)).digest("hex")}.archive`)
  assert(existsSync(bombRoot) && !existsSync(join(bombRoot, "payload.txt")), "ratio bomb was extracted before rejection")

  // Lie in the central-directory declared size; the defense-in-depth walk must still clean up.
  const lieInput = join(root, "lie-input"); mkdirSync(lieInput); writeFileSync(join(lieInput, "payload.txt"), "y".repeat(4096))
  const lie = join(root, "lie.zip"); assert(Bun.spawnSync(["zip", "-q", "-r", lie, "."], { cwd: lieInput }).exitCode === 0, "could not create lying-size fixture")
  const lieBytes = readFileSync(lie), central = Buffer.from("PK\x01\x02", "binary"), centralAt = lieBytes.indexOf(central)
  assert(centralAt >= 0, "zip central directory missing")
  lieBytes.writeUInt32LE(1, centralAt + 24); writeFileSync(lie, lieBytes)
  const lieCache = join(root, "lie-cache"), lieHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-lie"), cacheDir: lieCache, enabledKinds: ["archive"], maxArchiveBytes: 1, maxCompressionRatio: 100000 })
  const liePart = part({ filename: "lie.zip", path: lie, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(lie)).digest("hex") })
  const lieContainer = { parts: [liePart] }; await lieHooks["chat.message"]!({}, lieContainer)
  assert(lieContainer.parts[0].text.includes("maxArchiveBytes"), "post-extraction byte walk did not catch lying size")
  const lieRoot = join(lieCache, `${createHash("sha256").update(readFileSync(lie)).digest("hex")}.archive`)
  assert(!existsSync(lieRoot), "failed post-extraction expansion was not cleaned up")

  // Terminal ordinary-failure and uncertainty markers are both idempotent.
  let retryCalls = 0
  const retryHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-retry"), cacheDir: join(root, "retry-cache"), extractors: { pdf: async () => { retryCalls++; return "retry" } } })
  for (const marker of ["[media-preprocess failed: kind=pdf reason=old]", "[media-preprocess uncertain: kind=pdf extractor=pdf_tool.read-text reason=empty output]"]) {
    const retryPart = part(manifest(), `retry-${retryCalls}`); retryPart.text += `\n${marker}`
    const retryContainer = { parts: [retryPart] }; await retryHooks["chat.message"]!({}, retryContainer)
  }
  assert(retryCalls === 0, `terminal marker was re-extracted (${retryCalls})`)

  // Every emitted marker is terminal for both hook paths. Keep this list
  // coupled to the plugin's centralized marker set so future additions are
  // automatically covered by the idempotency check.
  let markerCalls = 0
  const markerHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-marker"), cacheDir: join(root, "marker-cache"), extractors: { pdf: async () => { markerCalls++; return "must not run" } } })
  for (const [name, marker] of Object.entries(MARKERS)) {
    const markerPart = part({ ...manifest(), sha256: `${String(name).charCodeAt(0).toString(16).padStart(2, "0")}${"d".repeat(62)}` }, `marker-${name}`)
    markerPart.text += `\n${marker}`
    const before = markerPart.text
    const chatContainer = { parts: [markerPart] }
    await markerHooks["chat.message"]!({}, chatContainer)
    assert(chatContainer.parts[0].text === before, `${name} marker changed in chat hook`)
    const transformOutput = { messages: [{ parts: [markerPart] }] }
    await markerHooks["experimental.chat.messages.transform"]!({}, transformOutput)
    assert(transformOutput.messages[0].parts[0].text === before, `${name} marker changed in transform hook`)
  }
  assert(markerCalls === 0, `centralized marker was re-extracted (${markerCalls})`)

  // Image extraction is unconditional; model modality declarations do not gate it.
  let imageCalls = 0
  const imageHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-image"), cacheDir: join(root, "image-cache"), extractors: {
    image: async () => { imageCalls++; return "image extracted" },
  } })
  const imagePart = part({ ...manifest("image", "/x.png"), mime: "image/png", filename: "x.png" }, "image-unconditional")
  const imageContainer = { parts: [imagePart] }
  await imageHooks["chat.message"]!({}, imageContainer)
  assert(imageCalls === 1 && imageContainer.parts[0].text.includes("image extracted"), "image extraction was gated")

  // Top-level text manifests are extracted by both hook paths and remain idempotent.
  const textPath = join(root, "top-level.txt")
  writeFileSync(textPath, "top-level text attachment contents")
  const textHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-text"), cacheDir: join(root, "text-cache") })
  const textManifest = { filename: "top-level.txt", path: textPath, mime: "text/plain", media_kind: "text", sha256: createHash("sha256").update(readFileSync(textPath)).digest("hex") }
  const textChat = { parts: [part(textManifest, "text-chat")] }
  await textHooks["chat.message"]!({}, textChat)
  const textChatOnce = textChat.parts[0].text
  assert(textChatOnce.includes("[media-preprocess text-file:") && textChatOnce.includes(`path=${textPath}`) && !textChatOnce.includes("top-level text attachment contents"), "top-level text was not reduced to a path-only manifest in chat hook")
  await textHooks["chat.message"]!({}, textChat)
  assert(textChat.parts[0].text === textChatOnce, "top-level text chat extraction was not idempotent")
  const textTransform = { messages: [{ parts: [part(textManifest, "text-transform")] }] }
  await textHooks["experimental.chat.messages.transform"]!({}, textTransform)
  const textTransformOnce = textTransform.messages[0].parts[0].text
  assert(textTransformOnce.includes("[media-preprocess text-file:") && textTransformOnce.includes(`path=${textPath}`) && !textTransformOnce.includes("top-level text attachment contents"), "top-level text was not reduced to a path-only manifest in transform hook")
  await textHooks["experimental.chat.messages.transform"]!({}, textTransform)
  assert(textTransform.messages[0].parts[0].text === textTransformOnce, "top-level text transform extraction was not idempotent")

  // Expansion directories are private, not merely the expansion root.
  const nestedInput = join(root, "nested-input"); mkdirSync(join(nestedInput, "one", "two"), { recursive: true }); writeFileSync(join(nestedInput, "one", "two", "file.txt"), "nested")
  const nestedDirArchive = join(root, "nested-dir.zip"); assert(Bun.spawnSync(["zip", "-q", "-r", nestedDirArchive, "."], { cwd: nestedInput }).exitCode === 0, "could not create nested directory fixture")
  const nestedDirHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-nested-dir"), cacheDir: join(root, "nested-dir-cache"), enabledKinds: ["archive"], maxCompressionRatio: 100000 })
  const nestedDirPart = part({ filename: "nested-dir.zip", path: nestedDirArchive, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(nestedDirArchive)).digest("hex") })
  const nestedDirContainer = { parts: [nestedDirPart] }; await nestedDirHooks["chat.message"]!({}, nestedDirContainer)
  const nestedDirRoot = join(root, "nested-dir-cache", `${createHash("sha256").update(readFileSync(nestedDirArchive)).digest("hex")}.archive`)
  assert((statSync(join(nestedDirRoot, "one")).mode & 0o777) === 0o700 && (statSync(join(nestedDirRoot, "one", "two")).mode & 0o777) === 0o700, "nested expansion directories are not 0700")

  // Nested archives are surfaced but deliberately not expanded.
  const inner = join(root, "inner.zip"); assert(Bun.spawnSync(["zip", "-q", inner, "-"], { input: "inner" }).exitCode !== 0 || existsSync(inner), "nested archive fixture failed")
  const nestedArchiveInput = join(root, "nested-archive-input"); mkdirSync(nestedArchiveInput); writeFileSync(join(nestedArchiveInput, "inner.zip"), readFileSync(inner))
  const nestedArchive = join(root, "nested-archive.zip"); assert(Bun.spawnSync(["zip", "-q", "-r", nestedArchive, "."], { cwd: nestedArchiveInput }).exitCode === 0, "could not create nested archive fixture")
  const nestedArchiveHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-nested-archive"), cacheDir: join(root, "nested-archive-cache"), enabledKinds: ["archive"], maxCompressionRatio: 100000 })
  const nestedArchivePart = part({ filename: "nested-archive.zip", path: nestedArchive, mime: "application/zip", media_kind: "archive", sha256: createHash("sha256").update(readFileSync(nestedArchive)).digest("hex") })
  const nestedArchiveContainer = { parts: [nestedArchivePart] }; await nestedArchiveHooks["chat.message"]!({}, nestedArchiveContainer)
  assert(nestedArchiveContainer.parts[0].text.includes('"handling":"nested-archive-skipped"'), "nested archive handling was not surfaced")

  // Multi-column PDF page images: generate a 3-page 2-column PDF and
  // verify the plugin produces per-page WebP images alongside extracted text.
  const multiPdf = join(root, "multi-column.pdf")
  const multiPy = join(root, "gen-multi-pdf.py")
  writeFileSync(multiPy, [
    'import fitz, sys',
    'd = fitz.open()',
    'for i in range(3):',
    '    p = d.new_page()',
    '    p.insert_text((50,80),"North Region Widget A: $1,200",fontsize=11)',
    '    p.insert_text((50,100),"North Region Widget B: $3,400",fontsize=11)',
    '    p.insert_text((50,130),"North Total: $4,600",fontsize=12)',
    '    p.insert_text((310,80),"South Region Widget C: $2,100",fontsize=11)',
    '    p.insert_text((310,100),"South Region Widget D: $800",fontsize=11)',
    '    p.insert_text((310,130),"South Total: $2,900",fontsize=12)',
    'd.save(sys.argv[1])',
  ].join("\n"))
  const py = process.env.MEDIA_GUARD_PYTHON ?? [join(import.meta.dir, "..", ".venv/bin/python"), join(import.meta.dir, "..", "scripts/.venv/bin/python")].find(existsSync) ?? Bun.which("python3") ?? (() => { throw new Error("python3 is required; install Python 3 or set MEDIA_GUARD_PYTHON") })()
  assert(Bun.spawnSync([py, multiPy, multiPdf]).exitCode === 0 && existsSync(multiPdf), "could not generate multi-column PDF")
  const multiSha = createHash("sha256").update(readFileSync(multiPdf)).digest("hex")
  const multiPdfHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-multi"), cacheDir: join(root, "multi-cache"), extractors: { pdf: async () => "tabular data extracted" } })
  const multiPart = part({ filename: "multi.pdf", path: multiPdf, mime: "application/pdf", media_kind: "pdf", sha256: multiSha }, "multi-pdf")
  const multiContainer = { parts: [multiPart] }
  await multiPdfHooks["chat.message"]!({}, multiContainer)
  const multiText = multiContainer.parts[0].text
  assert(multiText.includes("[media-preprocess pdf-pages: count=3"), "pdf-pages count marker missing")
  assert(multiText.includes("Dispatch \`vision-reader\` via \`task\`"), "vision-reader dispatch missing")
  assert(multiText.includes("page-0001.webp") && multiText.includes("page-0003.webp"), "page image paths missing")
  assert(multiText.includes("tabular data extracted"), "text extraction broke")
  const pageDir = join(join(root, "multi-cache"), `${multiSha}.pdfpages`)
  assert(existsSync(pageDir), "page cache dir missing")
  assert((statSync(join(pageDir, ".complete")).mode & 0o777) === 0o600, "PDF completion marker is not 0600")
  const webpFiles = readdirSync(pageDir).filter(f => f.endsWith(".webp")).sort()
  assert(webpFiles.length === 3, "expected 3 page images")
  for (const w of webpFiles) {
    const size = statSync(join(pageDir, w)).size
    assert((statSync(join(pageDir, w)).mode & 0o777) === 0o600, `page ${w} is not 0600`)
    assert(size < 500 * 1024, `page ${w} exceeds 500KB (${size} bytes)`)
    assert(size > 0, `page ${w} is empty`)
  }
  const webpHead = readFileSync(join(pageDir, webpFiles[0])).subarray(0, 12)
  assert(webpHead.slice(0, 4).toString() === "RIFF" && webpHead.slice(8, 12).toString() === "WEBP", "page image is not valid WebP")
  console.log("PASS multi-column PDF: 3 pages, images under 500KB, text preserved")
  console.log("PASS multi-column PDF page image sizes: " + webpFiles.map(f => statSync(join(pageDir, f)).size + "B").join(", "))

  // pdfPages marker is terminal for idempotency
  const multiAgain = multiText
  await multiPdfHooks["chat.message"]!({}, multiContainer)
  assert(multiContainer.parts[0].text === multiAgain, "multi-column pdf idempotency failed")

  // Fail-safe: when pages rendering fails (bad path), text extraction still works
  const badPdf = join(root, "bad.pdf")
  writeFileSync(badPdf, "not a real pdf")
  const badHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-bad"), cacheDir: join(root, "bad-cache"), extractors: { pdf: async () => "despite rasterization failure" } })
  const badPart = part({ filename: "bad.pdf", path: badPdf, mime: "application/pdf", media_kind: "pdf", sha256: createHash("sha256").update(readFileSync(badPdf)).digest("hex") })
  const badContainer = { parts: [badPart] }
  await badHooks["chat.message"]!({}, badContainer)
  const badText = badContainer.parts[0].text
  assert(badText.includes("[media-preprocess pdf-pages-failed:"), "pdf pages failure marker missing: " + badText)
  assert(badText.includes("despite rasterization failure"), "text extraction damaged by pages failure")
  console.log("PASS pdf pages fail-safe: text path still succeeded")


  // Video keyframe extraction: multi-scene video gets >=1 frame per scene cut
  const video = join(root, "multiscene.mp4")
  const ffmpeg = Bun.which("ffmpeg") ?? (() => { throw new Error("ffmpeg is required; install it or set PATH") })()
  // Create 3 distinct 2-second scenes (testsrc, red, blue) concatenated
  const scene1 = join(root, "scene1.mp4")
  const scene2 = join(root, "scene2.mp4")
  const scene3 = join(root, "scene3.mp4")
  assert(Bun.spawnSync([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", scene1]).exitCode === 0, "scene1 gen failed")
  assert(Bun.spawnSync([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:duration=2:size=320x240:rate=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", scene2]).exitCode === 0, "scene2 gen failed")
  assert(Bun.spawnSync([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:duration=2:size=320x240:rate=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", scene3]).exitCode === 0, "scene3 gen failed")
  const concatList = join(root, "concat.txt")
  writeFileSync(concatList, `file '${scene1}'\nfile '${scene2}'\nfile '${scene3}'\n`)
  assert(Bun.spawnSync([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concatList, "-c", "copy", video]).exitCode === 0, "concat failed")

  const videoSha = createHash("sha256").update(readFileSync(video)).digest("hex")
  const videoHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-video"), cacheDir: join(root, "video-cache"), extractors: { video: async () => "video transcript" } })
  const videoPart = part({ filename: "multiscene.mp4", path: video, mime: "video/mp4", media_kind: "video", sha256: videoSha })
  const videoContainer = { parts: [videoPart] }
  await videoHooks["chat.message"]!({}, videoContainer)
  const videoText = videoContainer.parts[0].text
  assert(videoText.includes("[media-preprocess video-keyframes:"), "video-keyframes marker missing: " + videoText)
  assert(videoText.includes("Dispatch \`vision-reader\` via \`task\`"), "vision-reader dispatch missing from video keyframes")
  // Extract frame count from marker
  const frameMatch = videoText.match(/video-keyframes: count=(\d+)/)
  assert(frameMatch, "could not parse frame count from marker: " + videoText)
  const frameCount = parseInt(frameMatch[1], 10)
  assert(frameCount >= 2, `expected at least 2 frames (one per scene cut), got ${frameCount}`)
  // Verify WebP output files exist and have valid headers
  const videoCacheDir = join(root, "video-cache")
  const keyframeDirEntries = readdirSync(videoCacheDir).filter(e => e.includes(".keyframes"))
  assert(keyframeDirEntries.length === 1, "expected exactly one .keyframes cache dir")
  const keyframeDir = join(videoCacheDir, keyframeDirEntries[0])
  assert((statSync(join(keyframeDir, ".complete")).mode & 0o777) === 0o600, "video completion marker is not 0600")
  const videoWebpFiles = readdirSync(keyframeDir).filter(f => f.endsWith(".webp")).sort()
  assert(videoWebpFiles.length === frameCount, `expected ${frameCount} WebP files, got ${videoWebpFiles.length}`)
  for (const w of videoWebpFiles) {
    const size = statSync(join(keyframeDir, w)).size
    assert((statSync(join(keyframeDir, w)).mode & 0o777) === 0o600, `frame ${w} is not 0600`)
    assert(size > 0 && size < 500 * 1024, `frame ${w} size ${size} out of range`)
    const head = readFileSync(join(keyframeDir, w)).subarray(0, 12)
    assert(head.slice(0, 4).toString() === "RIFF" && head.slice(8, 12).toString() === "WEBP", `frame ${w} is not valid WebP`)
  }
  console.log(`PASS video keyframes: ${frameCount} frames, WebP sizes: ${videoWebpFiles.map(f => statSync(join(keyframeDir, f)).size + "B").join(", ")}`)

  // Transcript path unaffected: text extraction still runs and is present
  assert(videoText.includes("video transcript"), "video transcript extraction broken by keyframes")
  console.log("PASS video transcript path unaffected by keyframes")

  // Fail-safe: corrupt video still produces transcript, emits failure marker, no throw
  const corruptVideo = join(root, "corrupt.mp4")
  writeFileSync(corruptVideo, "not a video")
  const corruptSha = createHash("sha256").update(readFileSync(corruptVideo)).digest("hex")
  const corruptHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-corrupt"), cacheDir: join(root, "corrupt-cache"), extractors: { video: async () => "transcript despite corrupt video" } })
  const corruptVideoPart = part({ filename: "corrupt.mp4", path: corruptVideo, mime: "video/mp4", media_kind: "video", sha256: corruptSha })
  const corruptVideoContainer = { parts: [corruptVideoPart] }
  await corruptHooks["chat.message"]!({}, corruptVideoContainer)
  const corruptText = corruptVideoContainer.parts[0].text
  assert(corruptText.includes("[media-preprocess video-keyframes-failed:"), "video-keyframes-failed marker missing: " + corruptText)
  assert(corruptText.includes("transcript despite corrupt video"), "text extraction damaged by keyframe failure")
  console.log("PASS video keyframes fail-safe: transcript path still succeeded")

  console.log("media-preprocess tests: all assertions passed")
} finally {
  rmSync(root, { recursive: true, force: true })
}
