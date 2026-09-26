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
  assert.deepEqual(registrations, ["prompt", "context"])
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

test.after(() => rmSync(root, { recursive: true, force: true }))
