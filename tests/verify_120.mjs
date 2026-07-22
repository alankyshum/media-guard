#!/usr/bin/env bun
/**
 * verify_120.mjs — 120-image regression test for media-guard.ts
 *
 * Generates 120 PNGs with OCR-able tokens, runs the media-guard transform
 * on a synthetic 120-part message, asserts all parts are replaced with
 * synthetic text, counts how many contain the OCR token, and proves the
 * in-memory cache (MAX_CACHE=512) serves the second run instantly.
 *
 * Usage: bun verify_120.mjs
 */
import { $ } from "bun"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { mkdtempSync, writeFileSync, readdirSync, statSync, existsSync } from "node:fs"
import { spawnSync } from "node:child_process"

// Resolve paths relative to __tests__
const TEST_DIR = import.meta.dir
const PLUGIN_FILE = join(TEST_DIR, "..", "media-guard.ts")
const GEN_SCRIPT = join(TEST_DIR, "gen_images.py")
process.env.OCR_VLM_ENABLED = "0"

// ---- 1. Ensure tesseract is available ----
{
  const r = spawnSync("which", ["tesseract"], { stdio: "pipe" })
  if (r.status !== 0) {
    console.error("tesseract not found; installing via brew...")
    spawnSync("brew", ["install", "tesseract"], { stdio: "inherit" })
  }
}

// ---- 2. Generate 120 test PNGs ----
const imgDir = mkdtempSync(join(tmpdir(), "media-guard-test-"))
console.log(`[harness] Generating 120 images in ${imgDir}`)
{
  const r = spawnSync("python3", [GEN_SCRIPT, imgDir, "120"], {
    stdio: ["inherit", "inherit", "inherit"],
    timeout: 60_000,
  })
  if (r.status !== 0) throw new Error("gen_images.py failed")
}
const files = readdirSync(imgDir).filter(f => f.endsWith(".png")).sort()
if (files.length !== 120) throw new Error(`Expected 120 images, got ${files.length}`)
console.log(`[harness] ${files.length} images ready`)

// Verify at least one OCRs correctly
{
  const sample = join(imgDir, files[0])
  const r = spawnSync("tesseract", [sample, "stdout"], { stdio: "pipe", timeout: 15_000 })
  const out = (r.stdout || "").toString().trim()
  if (!out.includes("MEDIAGUARD")) {
    console.warn(`[harness] WARNING: tesseract on sample image produced: "${out.slice(0, 80)}"`)
  } else {
    console.log(`[harness] tesseract sample OK: "${out.slice(0, 60)}..."`)
  }
}

// ---- 3. Import the plugin ----
// Use dynamic import since it's a .ts file with package deps
const mod = await import(PLUGIN_FILE)
const MediaGuardPlugin = mod.MediaGuardPlugin || mod.default

const opts = {
  agentKinds: ["image", "video"],
  agentTimeoutSec: 300,
  maxChars: 60_000,
  model: "base",
  timeoutSec: 180,
  concurrency: 6,
  batchThreshold: 24,
  transformBudgetSec: 240,
  extractorCache: false,
}

console.log(`[harness] Creating plugin instance`)
const hooks = await MediaGuardPlugin({ $ }, opts)
const transform = hooks["experimental.chat.messages.transform"]
if (typeof transform !== "function") throw new Error("transform hook not found")

// ---- 4. Build helper for output messages ----
function buildMessage(parts) {
  return {
    info: { role: "user" },
    parts,
  }
}

function buildFilePart(i, absPath) {
  const id = `p${i}`
  return {
    id,
    sessionID: "s",
    messageID: "m",
    type: "file",
    mime: "image/png",
    filename: `img${String(i).padStart(4, "0")}.png`,
    url: `file://${encodeURI(absPath)}`,
    source: { path: absPath },
  }
}

// ---- 5. Run #1 — should use deterministic OCR (batchThreshold triggers) ----
{
  const parts = [
    { type: "text", text: "Summarize these 120 scanned pages" },
    ...files.map((f, i) => buildFilePart(i, join(imgDir, f))),
  ]
  const output = { messages: [buildMessage(parts)] }
  process.env.MEDIA_GUARD_DEBUG = "1"

  const t0 = Date.now()
  await transform({}, output)
  const ms1 = Date.now() - t0

  // ---- 6. Assertions for run #1 ----
  const resultParts = output.messages[0].parts
  let fileCount = 0
  let textCount = 0
  let syntheticCount = 0
  let ocrTokenCount = 0

  for (const p of resultParts) {
    if (p.type === "file") fileCount++
    if (p.type === "text" && p.synthetic) {
      textCount++
      syntheticCount++
      if (p.text && p.text.includes("MEDIAGUARD OCR TOKEN")) ocrTokenCount++
    } else if (p.type === "text" && !p.synthetic) {
      // leading user text part
    }
  }

  const filePartsRemaining1 = fileCount

  console.log(`\n--- Run #1 (fresh) ---`)
  console.log(`  Duration: ${ms1}ms`)
  console.log(`  File parts remaining: ${fileCount}`)
  console.log(`  Synthetic text parts: ${syntheticCount}`)
  console.log(`  OCR token hits: ${ocrTokenCount}/120`)

  // ---- 7. Run #2 — cache test (new output, same file paths) ----
  // Build a completely fresh output object
  const parts2 = [
    { type: "text", text: "Summarize these 120 scanned pages" },
    ...files.map((f, i) => buildFilePart(i, join(imgDir, f))),
  ]
  const output2 = { messages: [buildMessage(parts2)] }

  const t2 = Date.now()
  await transform({}, output2)
  const ms2 = Date.now() - t2

  const resultParts2 = output2.messages[0].parts
  let fileCount2 = 0
  let syntheticCount2 = 0
  let ocrTokenCount2 = 0

  for (const p of resultParts2) {
    if (p.type === "file") fileCount2++
    if (p.type === "text" && p.synthetic) {
      syntheticCount2++
      if (p.text && p.text.includes("MEDIAGUARD OCR TOKEN")) ocrTokenCount2++
    }
  }

  const filePartsRemaining2 = fileCount2

  console.log(`\n--- Run #2 (cache) ---`)
  console.log(`  Duration: ${ms2}ms`)
  console.log(`  File parts remaining: ${fileCount2}`)
  console.log(`  Synthetic text parts: ${syntheticCount2}`)
  console.log(`  OCR token hits: ${ocrTokenCount2}/120`)

  // ---- 8. PASS/FAIL ----
  const pass =
    filePartsRemaining1 === 0 &&
    filePartsRemaining2 === 0 &&
    ocrTokenCount >= 100 &&
    ms1 < 240_000 &&
    ms2 < ms1 / 3

  console.log(`\n========================================`)
  console.log(`Run #1: ${ms1}ms | Run #2: ${ms2}ms | Cache ratio: ${(ms2/ms1*100).toFixed(1)}%`)
  console.log(`OCR hits: ${ocrTokenCount}/120 (run1) ${ocrTokenCount2}/120 (run2)`)
  console.log(`File parts remaining: ${filePartsRemaining1} (run1) ${filePartsRemaining2} (run2)`)
  console.log(`Result: ${pass ? "PASS" : "FAIL"}`)
  console.log(`========================================\n`)
}

// Cleanup temp dir
try { const rm = spawnSync("rm", ["-rf", imgDir], { stdio: "pipe" }); void rm } catch {}
