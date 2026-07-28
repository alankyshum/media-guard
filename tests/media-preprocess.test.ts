#!/usr/bin/env bun
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, realpathSync, lstatSync, readdirSync, statSync } from "node:fs"
import { createHash } from "node:crypto"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { detectedNativeKinds, MARKERS, MediaGuardPlugin, stripJsoncComments } from "../media-guard.ts"

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

try {
  const jsonc = JSON.parse(stripJsoncComments(readFileSync(join(import.meta.dir, "../../../config/opencode/opencode.jsonc"), "utf8")))
  assert(jsonc.provider["open-llm-proxy"].options.baseURL === "http://127.0.0.1:8765/v1", "JSONC parser corrupted URL string")

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
  const archiveHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-archive"), cacheDir: join(root, "archive-cache"), nativeKinds: ["image"], enabledKinds: ["pdf", "image", "audio", "video", "text", "archive"], extractors: { pdf: async () => "pdf", audio: async () => "audio", video: async () => "video" } })
  const archivePart = part({ filename: "bundle.zip", path: archivePath, mime: "application/zip", media_kind: "archive", sha256: archiveSha }, "archive")
  const archiveContainer = { parts: [archivePart] }
  await archiveHooks["chat.message"]!({}, archiveContainer)
  const archiveText = archiveContainer.parts[0].text
  assert(archiveText.includes("[media-preprocess archive: entries=2 expanded=2 skipped=0 truncated=false]"), "archive summary missing")
  assert(archiveText.includes("memo from archive") && archiveText.includes('"handling":"needs-agent"'), "archive partition missing")
  assert(archiveText.includes("Dispatch `vision-reader` via `task`") && archiveText.includes("diagram.png"), "vision routing missing")
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

  // Explicit nativeKinds skips only the selected top-level kind.
  let nativeCalls = 0
  const nativeHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-native"), cacheDir: join(root, "native-cache"), nativeKinds: ["image"], extractors: {
    image: async () => { nativeCalls++; return "should not run" },
    pdf: async () => { nativeCalls++; return "pdf still runs" },
  } })
  const nativeImage = part({ ...manifest("image", "/x.png"), mime: "image/png", filename: "x.png" }, "native-image")
  const nativePdf = part(manifest("pdf", "/x.pdf"), "native-pdf")
  const nativeContainer = { parts: [nativeImage, nativePdf] }
  await nativeHooks["chat.message"]!({}, nativeContainer)
  assert(nativeCalls === 1, `native image was extracted or PDF was skipped (${nativeCalls})`)
  assert(nativeContainer.parts[0].text.includes("[media-preprocess native-skip: kind=image reason=model accepts image input natively]"), "native-skip marker missing")
  assert(nativeContainer.parts[1].text.includes("pdf still runs"), "PDF was not extracted with image native")

  // [] is an explicit force-extract override and preserves the old behavior.
  let defaultCalls = 0
  const defaultHooks = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-native-default"), cacheDir: join(root, "native-default-cache"), nativeKinds: [], extractors: { image: async () => { defaultCalls++; return "image extracted" } } })
  const defaultImage = part({ ...manifest("image", "/x.png"), mime: "image/png", filename: "x.png", sha256: "e".repeat(64) }, "default-image")
  const defaultContainer = { parts: [defaultImage] }
  await defaultHooks["chat.message"]!({}, defaultContainer)
  assert(defaultCalls === 1 && defaultContainer.parts[0].text.includes("image extracted"), "empty nativeKinds did not extract image")

  // native-skip is terminal through both hook paths via processed().
  let skipCalls = 0
  const idempotentNative = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-native-idempotent"), cacheDir: join(root, "native-idempotent-cache"), nativeKinds: ["image"], extractors: { image: async () => { skipCalls++; return "must not run" } } })
  const alreadySkipped = part({ ...manifest("image", "/x.png"), mime: "image/png", filename: "x.png", sha256: "f".repeat(64) }, "already-skipped")
  alreadySkipped.text += `\n${MARKERS.nativeSkipped} kind=image reason=model accepts image input natively]`
  const alreadyText = alreadySkipped.text
  await idempotentNative["chat.message"]!({}, { parts: [alreadySkipped] })
  const transformAlready = { messages: [{ parts: [alreadySkipped] }] }
  await idempotentNative["experimental.chat.messages.transform"]!({}, transformAlready)
  assert(skipCalls === 0 && alreadySkipped.text === alreadyText, "native-skip marker was not idempotent")

  // A chain is native only when every member declares the modality.
  const chainInput = { model: { providerID: "synthetic", modelID: "[strong,weaker]" } }
  assert(detectedNativeKinds(chainInput, {
    strong: { modalities: { input: ["text", "image"] } },
    weaker: { modalities: { input: ["text"] } },
  })?.includes("image") === false, "chain incorrectly treated image as native")
  assert(detectedNativeKinds(chainInput, {
    strong: { modalities: { input: ["text", "image"] } },
    weaker: { modalities: { input: ["text", "image"] } },
  })?.includes("image") === true, "all-image chain was not treated as native")
  assert(detectedNativeKinds({ model: { providerID: "missing", modelID: "unknown" } }) === null, "malformed model info did not fail detection")

  // Detection failure defaults to extraction when no explicit nativeKinds is configured.
  let detectionCalls = 0
  const detectionFallback = await MediaGuardPlugin({}, { materializationDir: join(root, "guard-detection-fallback"), cacheDir: join(root, "detection-fallback-cache"), nativeKinds: [], extractors: { image: async () => { detectionCalls++; return "fallback extraction" } } })
  const detectionPart = part({ ...manifest("image", "/x.png"), mime: "image/png", filename: "x.png", sha256: "1".repeat(64) }, "detection-fallback")
  const detectionContainer = { parts: [detectionPart] }
  await detectionFallback["chat.message"]!({ model: { providerID: "missing", modelID: "unknown" } }, detectionContainer)
  assert(detectionCalls === 1 && !detectionContainer.parts[0].text.includes(MARKERS.nativeSkipped), "detection failure skipped instead of extracting")

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
  console.log("media-preprocess tests: all assertions passed")
} finally {
  rmSync(root, { recursive: true, force: true })
}
