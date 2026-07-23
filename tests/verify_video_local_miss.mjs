#!/usr/bin/env bun
import { $ } from "bun"
import { join } from "node:path"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"

const testDir = import.meta.dir
const videoPath = join(mkdtempSync(join(tmpdir(), "media-guard-video-miss-")), "failed.mp4")
writeFileSync(videoPath, "not a video")

const { MediaGuardPlugin } = await import(join(testDir, "..", "media-guard.ts"))
const hooks = await MediaGuardPlugin({ $, client: { app: { log: async () => {} } } }, {
  visionEnabled: true,
  visionBaseUrl: "http://127.0.0.1:9",
  visionTimeoutSec: 1,
  agentKinds: ["video"],
  extractorCache: false,
  timeoutSec: 1,
})
const output = { parts: [
  { type: "text", text: "transcribe this" },
  { type: "file", mime: "video/mp4", url: `file://${videoPath}`, filename: "failed.mp4" },
] }
await hooks["chat.message"]({}, output)

const part = output.parts[1]
const text = part?.text || ""
if (part?.type !== "text" || part?.synthetic !== true) throw new Error("failed video was not replaced with synthetic text")
if (output.parts.some(item => item.type === "file")) throw new Error("failed video left raw file part")
if (!text.includes("video") || !text.includes("Local extraction was unavailable")) throw new Error("video failure text missing")
if (/sticky.?notes|image/i.test(text)) throw new Error(`video failure text used image-only label: ${text}`)
console.log("PASS failed video local-vision miss -> synthetic video extraction/failure text")
