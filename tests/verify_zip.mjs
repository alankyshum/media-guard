#!/usr/bin/env bun
import { $ } from "bun"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { mkdtempSync, writeFileSync, readdirSync, rmSync } from "node:fs"
import { spawnSync } from "node:child_process"

const TEST_DIR = import.meta.dir
const PLUGIN_FILE = join(TEST_DIR, "..", "media-guard.ts")
const GEN_SCRIPT = join(TEST_DIR, "gen_images.py")
const dir = mkdtempSync(join(tmpdir(), "media-guard-zip-test-"))

try {
  const txtPath = join(dir, "notes.txt")
  const imgDir = join(dir, "images")
  const zipPath = join(dir, "bundle.zip")
  writeFileSync(txtPath, "ZIPGUARD_TEXT_TOKEN\n")

  const generated = spawnSync("python3", [GEN_SCRIPT, imgDir, "1"], { stdio: "inherit", timeout: 60_000 })
  if (generated.status !== 0) throw new Error("gen_images.py failed")
  const pngPath = join(imgDir, readdirSync(imgDir).find(f => f.endsWith(".png")))

  const zipped = spawnSync("zip", ["-j", zipPath, txtPath, pngPath], { stdio: "inherit" })
  if (zipped.status !== 0) throw new Error("zip failed")

  const mod = await import(PLUGIN_FILE)
  const MediaGuardPlugin = mod.MediaGuardPlugin || mod.default
  const hooks = await MediaGuardPlugin({ $ }, {
    agentKinds: ["image", "video"],
    visionEnabled: false,
    maxChars: 60000,
    model: "base",
    timeoutSec: 180,
    concurrency: 6,
    batchThreshold: 24,
    transformBudgetSec: 240,
  })
  const transform = hooks["experimental.chat.messages.transform"]
  if (typeof transform !== "function") throw new Error("transform hook not found")

  const output = {
    messages: [{
      info: { role: "user" },
      parts: [
        { type: "text", text: "inspect this archive" },
        {
          id: "z0",
          sessionID: "s",
          messageID: "m",
          type: "file",
          mime: "application/zip",
          filename: "bundle.zip",
          url: "file://" + encodeURI(zipPath),
          source: { path: zipPath },
        },
      ],
    }],
  }

  await transform({}, output)
  const parts = output.messages[0].parts
  const fileCount = parts.filter(p => p.type === "file").length
  const note = parts.find(p => p.synthetic && p.type === "text")?.text || ""
  const pass = fileCount === 0 && note.includes("ZIPGUARD_TEXT_TOKEN") && note.includes("ZIP ENTRY") && note.includes("MEDIAGUARD OCR TOKEN")
  console.log(`File parts remaining: ${fileCount}`)
  console.log(`Text token: ${note.includes("ZIPGUARD_TEXT_TOKEN")}`)
  console.log(`ZIP ENTRY: ${note.includes("ZIP ENTRY")}`)
  console.log(`OCR token: ${note.includes("MEDIAGUARD OCR TOKEN")}`)
  console.log(`Result: ${pass ? "PASS" : "FAIL"}`)
  if (!pass) process.exitCode = 1
} catch (e) {
  console.log(`Result: FAIL (${e instanceof Error ? e.message : String(e)})`)
  process.exitCode = 1
} finally {
  rmSync(dir, { recursive: true, force: true })
}
