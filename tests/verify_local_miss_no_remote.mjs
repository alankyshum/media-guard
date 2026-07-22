#!/usr/bin/env bun
import { $ } from "bun"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { mkdtempSync, readdirSync, readFileSync } from "node:fs"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const pluginPath = join(testDir, "..", "media-guard.ts")
const genScript = join(testDir, "gen_images.py")
const imageDir = mkdtempSync(join(tmpdir(), "media-guard-miss-"))
if (spawnSync("python3", [genScript, imageDir, "1"], { stdio: "pipe" }).status !== 0) throw new Error("fixture generation failed")
const imagePath = join(imageDir, readdirSync(imageDir).find(name => name.endsWith(".png")))
const dataUrl = `data:image/png;base64,${readFileSync(imagePath).toString("base64")}`

process.env.OCR_VLM_ENABLED = "0"
process.env.MEDIA_GUARD_DEBUG = "1"
const errors = []
const originalError = console.error
console.error = (...args) => errors.push(args.map(String).join(" "))
const { MediaGuardPlugin } = await import(pluginPath)
const hooks = await MediaGuardPlugin({ $ }, {
  visionEnabled: true,
  visionBaseUrl: "http://127.0.0.1:9",
  visionTimeoutSec: 1,
  agentKinds: ["image"],
  batchThreshold: 24,
  extractorCache: false,
})
const output = { parts: [
  { type: "text", text: "read this" },
  { type: "file", mime: "image/png", url: dataUrl, filename: "miss.png" },
] }
await hooks["chat.message"]({}, output)
console.error = originalError
const text = output.parts[1]?.text || ""
if (output.parts[1]?.type !== "text" || output.parts.some(part => part.type === "file")) throw new Error("media part was not replaced")
if (!text.includes("MEDIAGUARD OCR TOKEN 0001")) throw new Error("deterministic OCR fallback missing")
if (!errors.some(line => line.includes("fallback to deterministic local extraction"))) throw new Error("local miss telemetry missing")
if (errors.some(line => line.includes("opencode") || line.includes("remote-agent"))) throw new Error("remote fallback observed")
console.log("PASS forced local miss -> deterministic OCR; no remote fallback")
