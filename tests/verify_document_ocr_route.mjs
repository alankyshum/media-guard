#!/usr/bin/env bun
import { $ } from "bun"
import { join } from "node:path"
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { spawnSync } from "node:child_process"

const dir = mkdtempSync(join(tmpdir(), "media-guard-document-"))
const image = join(dir, "recovered-letter.png")
const script = "from PIL import Image,ImageDraw,ImageFont; im=Image.new('RGB',(1600,2200),'white'); d=ImageDraw.Draw(im); f=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',64); d.text((160,180),'Recovered Letter\\nReference 0001',font=f,fill='black',spacing=24); im.save(__import__('sys').argv[1])"
if (spawnSync("python3", ["-c", script, image], { encoding: "utf8" }).status !== 0) throw new Error("document fixture generation failed")

const originalFetch = globalThis.fetch
globalThis.fetch = async () => { throw new Error("non-document image vision route invoked") }
const { MediaGuardPlugin } = await import(join(import.meta.dir, "..", "media-guard.ts"))
const hooks = await MediaGuardPlugin({ $, client: {} }, { visionEnabled: true, agentKinds: ["image"], extractorCache: false, timeoutSec: 30 })
const output = { parts: [{ type: "text", text: "recover this letter" }, { type: "file", mime: "image/png", url: `file://${image}`, filename: "recovered-letter.png" }] }
await hooks["chat.message"]({}, output)
globalThis.fetch = originalFetch
const note = output.parts[1]?.text || ""
if (output.parts[1]?.type !== "text" || output.parts[1]?.synthetic !== true) throw new Error("document image was not replaced")
if (!note.includes("detected document image") || !note.includes("AUTHORITATIVE ORIGINAL IMAGE OCR/TEXT")) throw new Error("document did not take OCR-first route")
if (!note.includes("Recovered Letter") && !note.includes("Reference 0001")) throw new Error("document OCR text missing")
console.log("PASS detected document image -> OCR-first production extractor; non-document vision bypassed")

const slowDir = mkdtempSync(join(tmpdir(), "media-guard-slow-tesseract-"))
const slowTesseract = join(slowDir, "tesseract")
writeFileSync(slowTesseract, "#!/bin/sh\nsleep 2\n")
chmodSync(slowTesseract, 0o755)
const originalPath = process.env.PATH
process.env.PATH = `${slowDir}:${originalPath || ""}`
const slowHooks = await MediaGuardPlugin({ $, client: {} }, {
  visionEnabled: false,
  agentKinds: ["image"],
  extractorCache: false,
  timeoutSec: 0.25,
  transformBudgetSec: 5,
})
const slowOutput = { parts: [{ type: "text", text: "recover this letter" }, { type: "file", mime: "image/png", url: `file://${image}`, filename: "recovered-letter.png" }] }
const slowStarted = Date.now()
await slowHooks["chat.message"]({}, slowOutput)
const slowElapsed = Date.now() - slowStarted
process.env.PATH = originalPath
rmSync(slowDir, { recursive: true, force: true })
if (slowOutput.parts[1]?.type !== "text" || slowOutput.parts[1]?.synthetic !== true) throw new Error("slow route did not replace document image")
if (slowElapsed > 1200) throw new Error(`classifier + OCR exceeded shared per-file deadline: ${slowElapsed}ms`)
rmSync(dir, { recursive: true, force: true })
console.log(`PASS slow fake-Tesseract route respected shared classifier+OCR deadline (${slowElapsed}ms)`)
