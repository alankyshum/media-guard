#!/usr/bin/env bun
import assert from "node:assert/strict"
import test from "node:test"
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { basename, join } from "node:path"
import MediaGuardPlugin from "../media-guard.ts"

const root = mkdtempSync(join(tmpdir(), "media-guard-v2-"))
const hooks = new Map<string, (event: any) => unknown>()
const plugin = MediaGuardPlugin as any

async function setup(options: Record<string, unknown> = {}, sessionDirectories: Record<string, string> = {}) {
  const registrations: string[] = []
  const cleanup = await plugin.setup({
    options: { ...options, materializationDir: join(root, "materialized"), cacheDir: join(root, "cache") },
    location: { directory: root },
    session: {
      hook: async (name: string, callback: (event: any) => unknown) => {
        hooks.set(name, callback)
        registrations.push(name)
        return { dispose: async () => { hooks.delete(name) } }
      },
      get: async ({ sessionID }: { sessionID: string }) => ({ location: { directory: sessionDirectories[sessionID] ?? root } }),
    },
  })
  assert.deepEqual(registrations, ["prompt", "context", "compaction", "generate", "title"])
  return cleanup
}

test("V2 admission extracts supported media, sanitizes unsafe URIs, and preserves unconverted files", async () => {
  const source = join(root, "admission.pdf")
  const raw = Buffer.from("private pdf bytes")
  writeFileSync(source, raw)
  const cleanup = await setup({ enabledKinds: ["pdf"], extractors: { pdf: async () => "admission extraction result" } })
  try {
    const remoteURI = "https://example.invalid/remote.jpg"
    const retainedURI = "file:///tmp/notes.unknown"
    const event = { sessionID: "admission-session", prompt: { text: "summarize", files: [
      { uri: `file://${source}`, name: basename(source) },
      { uri: "file://evil.invalid/etc/passwd", name: "hostile.pdf" },
      { uri: `data:image/png;base64,${Buffer.from("image bytes").toString("base64")}`, name: "pixel.png" },
      { uri: remoteURI, name: "remote.jpg" },
      { uri: retainedURI, name: "notes.unknown" },
    ] } }
    await hooks.get("prompt")!(event)
    assert.deepEqual(event.prompt.files, [{ uri: retainedURI, name: "notes.unknown" }])
    assert.match(event.prompt.text, /\[media-guard attachment manifest\]/)
    assert.match(event.prompt.text, /admission extraction result/)
    const records = event.prompt.text.split("\n").filter((line) => line.startsWith("{")).map((line) => JSON.parse(line))
    const pdf = records.find((record) => record.filename === "admission.pdf")
    const hostile = records.find((record) => record.filename === "hostile.pdf")
    const data = records.find((record) => record.filename === "pixel.png")
    const remote = records.find((record) => record.filename === "remote.jpg")
    assert.equal(pdf.source, "local")
    assert.equal(pdf.sha256, (await import("node:crypto")).createHash("sha256").update(raw).digest("hex"))
    assert.match(event.prompt.text, /\[media-preprocess extracted: kind=pdf/)
    assert.equal(hostile.path, null)
    assert.equal(hostile.source, "error")
    assert.equal(data.source, "data-url")
    assert.equal(readFileSync(data.path, "utf8"), "image bytes")
    assert.equal(remote.path, null)
    assert.equal(remote.source, "remote")
    assert.doesNotMatch(event.prompt.text, /evil\.invalid|example\.invalid|data:image/)
  } finally {
    await cleanup?.()
  }
})

test("V2 context resolves relative attachment reads from each session location", async () => {
  const firstRoot = join(root, "workspace-one")
  const secondRoot = join(root, "workspace-two")
  mkdirSync(firstRoot)
  mkdirSync(secondRoot)
  writeFileSync(join(firstRoot, "receipt.txt"), "first private receipt")
  writeFileSync(join(secondRoot, "receipt.txt"), "second private receipt")
  const cleanup = await setup({}, { "session-one": firstRoot, "session-two": secondRoot })
  try {
    const readRequest = { type: "text", text: 'Called the Read tool with the following input:{"filePath":"receipt.txt"}' }
    const firstMessage = { parts: [readRequest, { type: "text", text: "first private receipt" }] }
    const secondMessage = { parts: [{ ...readRequest }, { type: "text", text: "second private receipt" }] }
    await hooks.get("context")!({ sessionID: "session-one", messages: [firstMessage] })
    await hooks.get("context")!({ sessionID: "session-two", messages: [secondMessage] })
    assert.match(firstMessage.parts[1].text, new RegExp(firstRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
    assert.match(secondMessage.parts[1].text, new RegExp(secondRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
    assert.doesNotMatch(firstMessage.parts[1].text, /workspace-two/)
    assert.doesNotMatch(secondMessage.parts[1].text, /workspace-one/)
  } finally {
    await cleanup?.()
  }
})

function filePart(bytes: Buffer, mime = "application/pdf", name = "nested.pdf") {
  return { type: "file", uri: `data:${mime};base64,${bytes.toString("base64")}`, mime, name }
}

function toolResult(value: unknown[]) {
  return { type: "tool-result", id: "call-read", name: "read", result: { type: "content", value } }
}

function manifest(part: unknown): { filename: string; path: string | null; size: number | null; error?: string } {
  assert.ok(part && typeof part === "object" && "type" in part && part.type === "text" && "text" in part && typeof part.text === "string")
  return JSON.parse(part.text.split("\n")[1])
}

test("V2 request hooks materialize nested tool files without changing call IDs or opaque JSON", async () => {
  const raw = Buffer.from("%PDF-1.4\nnested bytes\n%%EOF")
  const cleanup = await setup({ enabledKinds: [] })
  try {
    for (const hook of ["context", "compaction", "generate", "title"]) {
      const file = filePart(raw, "application/pdf", "/workspace/report.pdf")
      const value: unknown[] = [{ type: "text", text: "PDF read successfully" }, file]
      const result = toolResult(value)
      const opaque = { type: "tool-result", id: "json-call", name: "inspect", result: { type: "json", value: { content: [file] } } }
      const beforeOpaque = JSON.stringify(opaque)
      const event = { sessionID: "nested-session", messages: [{ role: "tool", content: [result, opaque] }] }
      await hooks.get(hook)!(event)
      const record = manifest(value[1])
      assert.equal(record.filename, "report.pdf")
      assert.equal(record.size, raw.length)
      assert.ok(record.path)
      assert.deepEqual(readFileSync(record.path), raw)
      assert.deepEqual(value[0], { type: "text", text: "PDF read successfully" })
      assert.equal(result.id, "call-read")
      assert.equal(result.name, "read")
      assert.equal(JSON.stringify(opaque), beforeOpaque)
      assert.doesNotMatch(JSON.stringify(result), /data:application\/pdf/)
      const once = JSON.stringify(event)
      await hooks.get(hook)!(event)
      assert.equal(JSON.stringify(event), once, "transformation is idempotent")
    }
  } finally { await cleanup?.() }
})

test("V2 context handles stored tool content and canonical media sources", async () => {
  const raw = Buffer.from("%PDF-1.4\nsource bytes\n%%EOF")
  const cleanup = await setup({ enabledKinds: [] })
  try {
    const stored: unknown[] = [filePart(raw)]
    const media = [
      { type: "media", filename: "bytes.pdf", media: { source: { type: "bytes", data: raw, mediaType: "application/pdf" } } },
      { type: "media", filename: "base64.pdf", media: { source: { type: "base64", data: raw.toString("base64"), mediaType: "application/pdf" } } },
      { type: "media", filename: "url.pdf", media: { source: { type: "url", url: filePart(raw).uri, mediaType: "application/pdf" } } },
    ]
    const parts: unknown[] = [...media, { type: "tool", name: "read", state: { status: "completed", content: stored } }]
    await hooks.get("context")!({ sessionID: "stored-session", messages: [{ content: parts }] })
    for (const part of [...parts.slice(0, 3), ...stored]) {
      const record = manifest(part)
      assert.ok(record.path)
      assert.deepEqual(readFileSync(record.path), raw)
    }
    const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0])
    const imageParts: unknown[] = [{ type: "media", filename: "image.jpg", media: { source: { type: "base64", data: jpeg.toString("base64"), mediaType: "image/jpeg" } } }]
    await hooks.get("context")!({ sessionID: "stored-session", messages: [{ content: imageParts }] })
    const image = manifest(imageParts[0])
    assert.ok(image.path, "base64 beginning with / must not be interpreted as a local path")
    assert.deepEqual(readFileSync(image.path), jpeg)
  } finally { await cleanup?.() }
})

test("nested corrupted PDFs become explicit errors while sibling attachments are preprocessed", async () => {
  const cleanup = await setup({ enabledKinds: ["text"] })
  try {
    const corrupt = { type: "file", mime: "application/pdf", name: "corrupt.pdf", uri: 'data:application/pdf;base64,JVBERi0x<PII type="PHONE" id="13"/>LjQ=' }
    const value: unknown[] = [corrupt, filePart(Buffer.from("nested extracted text"), "text/plain", "notes.txt")]
    const event = { sessionID: "corrupted-history", messages: [{ role: "tool", content: [toolResult(value)] }] }
    await hooks.get("context")!(event)
    assert.equal(manifest(value[0]).path, null)
    assert.equal(manifest(value[0]).filename, "corrupt.pdf")
    assert.match(manifest(value[0]).error ?? "", /invalid base64/)
    assert.match(JSON.stringify(value[1]), /\[media-preprocess text-file:/)
    const sibling = manifest(value[1])
    assert.ok(sibling.path)
    assert.equal(readFileSync(sibling.path, "utf8"), "nested extracted text")
    assert.doesNotMatch(JSON.stringify(event), /data:application\/pdf|<PII/)
  } finally { await cleanup?.() }
})

test("attachment count and byte budgets are shared across top-level and nested files", async () => {
  const raw = Buffer.from("12345678")
  for (const options of [
    { maxFilesPerTransform: 1 },
    { maxTotalMaterializedBytes: raw.length },
    { maxMaterializedBytes: raw.length - 1 },
  ]) {
    const cleanup = await setup({ ...options, enabledKinds: [] })
    try {
      const nested: unknown[] = [filePart(raw)]
      const parts: unknown[] = [filePart(raw), toolResult(nested)]
      await hooks.get("context")!({ sessionID: "limits", messages: [{ content: parts }] })
      const error = manifest(nested[0]).error ?? ""
      assert.match(error, /maxFilesPerTransform|maxTotalMaterializedBytes|maxMaterializedBytes/)
      if ("maxFilesPerTransform" in options || "maxMaterializedBytes" in options) assert.ok(manifest(parts[0]).error)
      else assert.equal(manifest(parts[0]).size, raw.length)
      assert.doesNotMatch(JSON.stringify(parts), /data:application\/pdf/)
    } finally { await cleanup?.() }
  }
})

test.after(() => rmSync(root, { recursive: true, force: true }))
