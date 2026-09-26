#!/usr/bin/env bun
// Attachment-manifest regression test.
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

const root = join(fileURLToPath(import.meta.url), "..", "..")
const { MediaGuardPlugin } = await import(join(root, "media-guard.ts"))
async function setup(options = {}) {
  let context
  await MediaGuardPlugin.setup({ options, location: { directory: root }, session: {
    hook: async (name, callback) => { if (name === "context") context = callback; return { dispose: async () => {} } },
    get: async () => ({ location: { directory: root } }),
  } })
  return { context: event => context({ sessionID: "test-session", messages: event.messages }) }
}
const dir = mkdtempSync(join(tmpdir(), "media-guard-test-"))
try {
  const local = join(dir, "receipt.txt")
  writeFileSync(local, "do not parse this")
  const data = "data:image/png;base64," + Buffer.from("image bytes").toString("base64")
  const output = { messages: [{ parts: [
    { id: "text-1", type: "text", text: "intent" },
    { id: "local-1", type: "file", filename: "../receipt.txt", mime: "application/pdf", source: { path: local }, url: "https://evil.invalid/raw" },
    { id: "data-1", type: "file", filename: "photo.png", mime: "image/png", url: data },
    { id: "remote-1", type: "file", filename: "remote.jpg", mime: "image/jpeg", url: "https://example.invalid/remote.jpg" },
  ] }] }
  const hooks = await setup({})
  if (typeof hooks.context !== "function") throw new Error("V2 context hook was not registered")

  const chatMessage = { parts: [
    { id: "chat-jpg", sessionID: "session-1", messageID: "message-1", type: "file", filename: "telegram/photo_2024-01-01_12-00-00.jpg", mime: "IMAGE/JPEG", url: data.replace("image/png", "image/jpeg") },
    { id: "chat-text", type: "text", text: "after media" },
  ] }
  await hooks.context({ messages: [{ parts: chatMessage.parts }] })
  if (chatMessage.parts[0].type !== "text" || !chatMessage.parts[0].synthetic) throw new Error("V2 context did not transform data URL")
  if (chatMessage.parts[0].id !== "chat-jpg" || chatMessage.parts[0].sessionID !== "session-1" || chatMessage.parts[0].messageID !== "message-1") throw new Error("V2 context identity was not preserved")
  const chatRecord = JSON.parse(chatMessage.parts[0].text.split("\n", 2)[1])
  if (chatRecord.filename !== "photo_2024-01-01_12-00-00.jpg" || chatRecord.source !== "data-url" || chatRecord.mime !== "image/jpeg") throw new Error("V2 context JPG manifest is incorrect")
  const parameterized = { parts: [{ id: "parameterized", type: "file", filename: "parameterized.png", mime: "  IMAGE/PNG ; charset=binary ", url: data }] }
  await hooks.context({ messages: [{ parts: parameterized.parts }] })
  if (parameterized.parts[0].type !== "text" || !parameterized.parts[0].synthetic) throw new Error("parameterized MIME was not intercepted")
  const parameterizedRecord = JSON.parse(parameterized.parts[0].text.split("\n", 2)[1])
  if (parameterizedRecord.mime !== "image/png" || parameterizedRecord.media_kind !== "image") throw new Error("parameterized MIME was not canonicalized")
  const noMatching = { parts: [{ id: "plain", type: "text", text: "unchanged" }] }
  await hooks.context({ messages: [{ parts: noMatching.parts }] })
  if (noMatching.parts[0].text !== "unchanged") throw new Error("V2 context changed output without raw media")

  await hooks.context({ messages: output.messages })
  const parts = output.messages[0].parts
  if (parts[0].text !== "intent" || parts.slice(1).some(p => p.type !== "text" || !p.synthetic)) throw new Error("parts not transformed")
  for (const part of parts.slice(1)) {
    if (part.id.endsWith("-1") === false) throw new Error("part ID was not preserved")
    for (const key of ["url", "source", "filename", "mime"]) if (key in part) throw new Error(`raw field retained: ${key}`)
    const text = part.text
    if (/do not parse this|image bytes/.test(text)) throw new Error("file content leaked")
    const record = JSON.parse(text.split("\n", 2)[1])
    if (!record.filename || !record.mime || !record.media_kind || !record.schema_version || !record.source) throw new Error("manifest incomplete")
    if ("inode" in record || "filesystem" in record || "original_filename" in record) throw new Error("excess metadata retained")
  }
  const dataRecord = JSON.parse(parts[2].text.split("\n", 2)[1])
  if (!readFileSync(dataRecord.path).equals(Buffer.from("image bytes"))) throw new Error("data URL not materialized")
  const localRecord = JSON.parse(parts[1].text.split("\n", 2)[1])
  if ("pointer" in localRecord) throw new Error("unrelated raw URL became local manifest pointer")
  const remoteRecord = JSON.parse(parts[3].text.split("\n", 2)[1])
  if (remoteRecord.source !== "remote" || remoteRecord.path !== null || "pointer" in remoteRecord || remoteRecord.error !== "remote attachment was not downloaded") throw new Error("remote URL downloaded or retained")
  rmSync(local)
  if (!readFileSync(JSON.parse(parts[1].text.split("\n", 2)[1]).path).equals(Buffer.from("do not parse this"))) throw new Error("staged local file did not survive source deletion")
  if ((statSync(dataRecord.path).mode & 0o077) !== 0 || (statSync(dataRecord.path).mode & 0o600) !== 0o600) throw new Error("staged file permissions are not 0600")
  const localhostFile = join(dir, "localhost.txt")
  writeFileSync(localhostFile, "localhost file")
  const localhost = { messages: [{ parts: [{ id: "localhost", type: "file", filename: "x", mime: "application/pdf", url: `file://localhost${localhostFile}` }] }] }
  await hooks.context({ messages: localhost.messages })
  if (localhost.messages[0].parts[0].type !== "text") throw new Error("localhost file URL was not staged")
  const hostile = { messages: [{ parts: [{ id: "hostile", type: "file", filename: "x", mime: "image/png", url: "file://evil.invalid/etc/passwd" }] }] }
  await hooks.context({ messages: hostile.messages })
  if (hostile.messages[0].parts[0].type !== "text") throw new Error("hostile file URL was not sanitized")
  const hostileRecord = JSON.parse(hostile.messages[0].parts[0].text.split("\n", 2)[1])
  if (hostileRecord.path !== null || hostileRecord.source !== "error" || "url" in hostileRecord || "source_path" in hostileRecord) throw new Error("hostile file URL leaked fields")
  const invalid = { messages: [{ parts: [{ id: "keep", type: "text", text: "keep" }, { id: "bad", type: "file", filename: "x", mime: "image/png", url: "data:image/png;base64,not-valid!" }] }] }
  await hooks.context({ messages: invalid.messages })
  if (invalid.messages[0].parts[0].type !== "text" || invalid.messages[0].parts[1].type !== "text") throw new Error("invalid data URL did not sanitize failure")
  const independent = { messages: [{ parts: [
    { id: "good", type: "file", filename: "good.png", mime: "image/png", url: data },
    { id: "bad", type: "file", filename: "bad.png", mime: "image/png", url: "data:image/png;base64,not-valid!" },
  ] }] }
  await hooks.context({ messages: independent.messages })
  if (independent.messages[0].parts[0].type !== "text" || independent.messages[0].parts[1].type !== "text") throw new Error("per-file failure affected independent processing")
  const independentGood = JSON.parse(independent.messages[0].parts[0].text.split("\n", 2)[1])
  const independentBad = JSON.parse(independent.messages[0].parts[1].text.split("\n", 2)[1])
  if (independentGood.source !== "data-url" || independentBad.source !== "error" || independentBad.path !== null) throw new Error("per-file manifests incorrect")

  const chatFailure = { parts: [
    { id: "failure", type: "file", filename: "failure.jpg", mime: "image/jpeg", url: "data:image/jpeg;base64,not-valid!" },
  ] }
  await hooks.context({ messages: [{ parts: chatFailure.parts }] })
  if (chatFailure.parts[0].type !== "text" || !chatFailure.parts[0].synthetic) throw new Error("chat.message failure was not sanitized")
  const failureRecord = JSON.parse(chatFailure.parts[0].text.split("\n", 2)[1])
  if (failureRecord.source !== "error" || failureRecord.path !== null || "url" in failureRecord) throw new Error("chat.message failure leaked raw media")
  const limited = await setup({ maxMaterializedBytes: 1 })
  const oversized = { messages: [{ parts: [{ id: "oversized", type: "file", filename: "x", mime: "image/png", url: data }] }] }
  await limited.context({ messages: oversized.messages })
  if (oversized.messages[0].parts[0].type !== "text") throw new Error("oversize data URL did not sanitize failure")
  const countLimited = await setup({ maxFilesPerTransform: 1 })
  const tooMany = { messages: [{ parts: [
    { id: "one", type: "file", filename: "x", mime: "image/png", url: data },
    { id: "two", type: "file", filename: "x", mime: "image/png", url: data },
  ] }] }
  await countLimited.context({ messages: tooMany.messages })
  if (tooMany.messages[0].parts.some(p => p.type !== "text")) throw new Error("file-count limit did not sanitize failure")
  const multiDir = join(dir, "multi-materialization")
  const multiLimited = await setup({ maxFilesPerTransform: 2, materializationDir: multiDir })
  const multiTooMany = { messages: [
    { parts: [{ id: "multi-one", type: "file", filename: "one.png", mime: "image/png", url: data }] },
    { parts: [{ id: "multi-two", type: "file", filename: "two.png", mime: "image/png", url: data }, { id: "multi-three", type: "file", filename: "three.png", mime: "image/png", url: data }] },
  ] }
  await multiLimited.context({ messages: multiTooMany.messages })
  if (multiTooMany.messages.flatMap(message => message.parts).some(part => part.type !== "text" || !part.synthetic)) throw new Error("multi-message file-count limit did not sanitize all matches")
  if (existsSync(multiDir) && readdirSync(multiDir).length !== 0) throw new Error("multi-message preflight staged files or left side effects")
  const chatLimited = await setup({ maxFilesPerTransform: 1, materializationDir: join(dir, "chat-limit") })
  const chatTooMany = { parts: [
    { id: "chat-one", type: "file", filename: "one.png", mime: "image/png", url: data },
    { id: "chat-two", type: "file", filename: "two.png", mime: "image/png", url: data },
  ] }
  await chatLimited.context({ messages: [{ parts: chatTooMany.parts }] })
  if (chatTooMany.parts.some(part => part.type !== "text" || !part.synthetic)) throw new Error("chat.message file-count preflight did not sanitize all matches")
  if (existsSync(join(dir, "chat-limit")) && readdirSync(join(dir, "chat-limit")).length !== 0) throw new Error("chat.message preflight staged files or left side effects")
  const totalLimited = await setup({ maxTotalMaterializedBytes: 5 })
  const tooMuchTotal = { messages: [{ parts: [
    { id: "total-one", type: "file", filename: "x", mime: "image/png", url: data },
    { id: "total-two", type: "file", filename: "x", mime: "image/png", url: data },
  ] }] }
  await totalLimited.context({ messages: tooMuchTotal.messages })
  if (tooMuchTotal.messages[0].parts.some(p => p.type !== "text")) throw new Error("total-byte limit did not sanitize failure")
  const overrideLimited = await setup({ maxMaterializedBytes: 1, maxFilesPerTransform: 1, maxTotalMaterializedBytes: 1 })
  const overrideParts = { messages: [{ parts: [{ id: "override", type: "file", filename: "x", mime: "image/png", url: data }] }] }
  await overrideLimited.context({ messages: overrideParts.messages })
  if (overrideParts.messages[0].parts[0].type !== "text") throw new Error("explicit limits did not override workspace limits")
  const workspaceLimited = await setup({})
  const workspaceParts = { messages: [{ parts: [{ id: "workspace", type: "file", filename: "x", mime: "image/png", url: data }] }] }
  await workspaceLimited.context({ messages: workspaceParts.messages })
  if (workspaceParts.messages[0].parts[0].type !== "text") throw new Error("workspace limits were not loaded")
  const symlinkDir = join(dir, "materialization-link")
  try {
    const { symlinkSync } = await import("node:fs")
    symlinkSync(dir, symlinkDir)
    await setup({ materializationDir: symlinkDir })
    throw new Error("symlink materialization directory accepted")
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes("symlinks")) throw error
  }
  console.log("media manifest test: PASS")
} finally {
  rmSync(dir, { recursive: true, force: true })
}
