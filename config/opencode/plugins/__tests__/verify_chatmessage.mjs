#!/usr/bin/env bun
/**
 * verify_chatmessage.mjs — prove the `chat.message` hook fixes silent-turn-death
 * for image attachments via the web UI (data: URLs).
 *
 * Asserts:
 *   (a) image file part converted to synthetic text part IN PLACE
 *   (b) zero file-type parts remain after hook
 *   (c) OCR token present on deterministic (batchThreshold=0) instance
 *   (d) stable content-hashed path: 2nd call to deterministic instance is
 *       dramatically faster (in-memory cache hit) and exactly one hashed file
 *       exists on disk for the image content.
 *
 * Usage: rm -rf /tmp/opencode-media-guard-data "${TMPDIR:-/tmp}/opencode-media-cache" /tmp/opencode-media-cache
 *        bun verify_chatmessage.mjs 2>&1
 */
import { $ } from "bun"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { mkdtempSync, readFileSync, readdirSync, existsSync, statSync } from "node:fs"
import { spawnSync } from "node:child_process"

const TEST_DIR = import.meta.dir
const PLUGIN_FILE = join(TEST_DIR, "..", "media-guard.ts")
const GEN_SCRIPT = join(TEST_DIR, "gen_images.py")

// ---- 0. Wipe disk caches ----
const DATAURL_DIR = join(tmpdir(), "opencode-media-guard-data")
const MEDIA_CACHE = join(tmpdir(), "opencode-media-cache")
const ALT_CACHE = "/tmp/opencode-media-cache"
for (const d of [DATAURL_DIR, MEDIA_CACHE, ALT_CACHE]) {
  try { spawnSync("rm", ["-rf", d], { stdio: "pipe" }) } catch {}
}
console.log("[harness] Disk caches cleared")

// ---- 1. Ensure tesseract ----
{
  const r = spawnSync("which", ["tesseract"], { stdio: "pipe" })
  if (r.status !== 0) {
    console.error("[harness] tesseract not found; installing via brew...")
    spawnSync("brew", ["install", "tesseract"], { stdio: "inherit" })
  }
}

// ---- 2. Generate ONE test PNG ----
const imgDir = mkdtempSync(join(tmpdir(), "media-guard-chatmsg-"))
console.log(`[harness] Generating 1 image in ${imgDir}`)
{
  const r = spawnSync("python3", [GEN_SCRIPT, imgDir, "1"], {
    stdio: ["inherit", "inherit", "inherit"],
    timeout: 60_000,
  })
  if (r.status !== 0) throw new Error("gen_images.py failed")
}
const files = readdirSync(imgDir).filter(f => f.endsWith(".png")).sort()
if (files.length !== 1) throw new Error(`Expected 1 image, got ${files.length}`)

const imgPath = join(imgDir, files[0])
const imgBytes = readFileSync(imgPath)
const imgB64 = imgBytes.toString("base64")
const dataUrl = `data:image/png;base64,${imgB64}`
console.log(`[harness] Image: ${imgPath} (${imgBytes.length} bytes)`)
console.log(`[harness] Data URL length: ${dataUrl.length} chars`)

// ---- 3. Import the plugin ----
const mod = await import(PLUGIN_FILE)
const MediaGuardPlugin = mod.MediaGuardPlugin || mod.default

const defaultOpts = {
  agentKinds: ["image", "video"],
  agentTimeoutSec: 300,
  maxChars: 60_000,
  model: "base",
  timeoutSec: 180,
  concurrency: 6,
  batchThreshold: 24,
  transformBudgetSec: 240,
}

const deterministicOpts = {
  ...defaultOpts,
  batchThreshold: 0,  // force deterministic always
}

console.log(`[harness] Creating plugin instances`)

// Instance A: default opts — for invariant checks (a,b)
// Instance B: batchThreshold=0 — for OCR token assertion (c) and cache speed (d)
process.env.MEDIA_GUARD_DEBUG = "1"
const hooksA = await MediaGuardPlugin({ $ }, defaultOpts)
const hooksB = await MediaGuardPlugin({ $ }, deterministicOpts)

const chatMessageA = hooksA["chat.message"]
const chatMessageB = hooksB["chat.message"]
if (typeof chatMessageA !== "function") throw new Error("chat.message hook not found on instance A")
if (typeof chatMessageB !== "function") throw new Error("chat.message hook not found on instance B")

// ---- 4. Build test output objects ----
function buildOutput() {
  return {
    message: { id: "msg_x", sessionID: "s", role: "user" },
    parts: [
      { id: "prt_t", sessionID: "s", messageID: "msg_x", type: "text", text: "describe this" },
      { id: "prt_f", sessionID: "s", messageID: "msg_x", type: "file", mime: "image/png", url: dataUrl, filename: "image.png" },
    ],
  }
}

// ---- 5. Run instance A (default opts) — type/synthetic/no-file-part invariants ----
const outputA = buildOutput()
const tA0 = Date.now()
await chatMessageA({ sessionID: "s", messageID: "msg_x" }, outputA)
const msA = Date.now() - tA0

const partsA = outputA.parts
const checkA_type = partsA[1].type === "text" && partsA[1].synthetic === true
const checkA_noFile = !partsA.some(p => p?.type === "file" && p.mime?.startsWith("image/"))

console.log(`\n--- Instance A (default opts) ---`)
console.log(`  Duration: ${msA}ms`)
console.log(`  (a) part[1] is synthetic text: ${checkA_type ? "PASS" : "FAIL"}  (got type=${partsA[1]?.type}, synthetic=${partsA[1]?.synthetic})`)
console.log(`  (b) no image file parts remain: ${checkA_noFile ? "PASS" : "FAIL"}`)
if (partsA[1]?.text) {
  console.log(`  text preview: ${partsA[1].text.slice(0, 120)}...`)
}

// ---- 6. Run instance B (deterministic) — OCR token + cache speed + hashed file proof ----
// Run #1 (cold)
const outputB1 = buildOutput()
const tB1 = Date.now()
await chatMessageB({ sessionID: "s", messageID: "msg_x" }, outputB1)
const msB1 = Date.now() - tB1

const partsB1 = outputB1.parts
const checkB_type = partsB1[1].type === "text" && partsB1[1].synthetic === true
const checkB_noFile = !partsB1.some(p => p?.type === "file" && p.mime?.startsWith("image/"))
const ocrText = partsB1[1]?.text || ""
const hasToken = ocrText.includes("MEDIAGUARD OCR TOKEN 0001")

console.log(`\n--- Instance B (deterministic, batchThreshold=0) Run #1 (cold) ---`)
console.log(`  Duration: ${msB1}ms`)
console.log(`  type+synthetic: ${checkB_type ? "PASS" : "FAIL"}`)
console.log(`  no file parts: ${checkB_noFile ? "PASS" : "FAIL"}`)
console.log(`  (c) OCR token present: ${hasToken ? "PASS" : "FAIL"}  (text length: ${ocrText.length})`)
if (!hasToken) {
  console.log(`  FALLBACK: text length>0: ${ocrText.length > 0 ? "PASS" : "FAIL"}`)
}
console.log(`  text preview: ${ocrText.slice(0, 120)}...`)

// Run #2 (same data URL → cache hit)
const outputB2 = buildOutput()
const tB2 = Date.now()
await chatMessageB({ sessionID: "s", messageID: "msg_x" }, outputB2)
const msB2 = Date.now() - tB2

const partsB2 = outputB2.parts
const ocrText2 = partsB2[1]?.text || ""
const hasToken2 = ocrText2.includes("MEDIAGUARD OCR TOKEN 0001")

console.log(`\n--- Instance B Run #2 (cache) ---`)
console.log(`  Duration: ${msB2}ms`)
console.log(`  OCR token present: ${hasToken2 ? "PASS" : "FAIL"}`)

// (d) cache speed proof
const cacheSpeedup = msB2 < msB1 / 2
console.log(`  (d) msB1=${msB1}ms  msB2=${msB2}ms  speedup: ${(msB1 / Math.max(msB2, 1)).toFixed(1)}x`)
console.log(`  cache fast (msB2 < msB1/2): ${cacheSpeedup ? "PASS" : "FAIL"}`)

// (d) hashed-file count: exactly one file under DATAURL_DIR
let hashedCount = 0
let hashedPaths = []
if (existsSync(DATAURL_DIR)) {
  const entries = readdirSync(DATAURL_DIR)
  hashedCount = entries.length
  hashedPaths = entries
}
console.log(`  hashed files under ${DATAURL_DIR}: ${hashedCount}`)
const singleHashedFile = hashedCount === 1
console.log(`  exactly one hashed file: ${singleHashedFile ? "PASS" : "FAIL"}`)
if (hashedPaths.length > 0) {
  const fp = join(DATAURL_DIR, hashedPaths[0])
  const st = statSync(fp)
  console.log(`  file: ${hashedPaths[0]} (${st.size} bytes)`)
}

// ---- 7. Final PASS/FAIL ----
const pass =
  checkA_type && checkA_noFile &&
  checkB_type && checkB_noFile &&
  hasToken &&
  cacheSpeedup &&
  singleHashedFile

console.log(`\n========================================`)
console.log(`  Instance A: ${msA}ms | (a) ${checkA_type ? "PASS" : "FAIL"} (b) ${checkA_noFile ? "PASS" : "FAIL"}`)
console.log(`  Instance B: ${msB1}ms / ${msB2}ms | (c) ${hasToken ? "PASS" : "FAIL"} (d-cache) ${cacheSpeedup ? "PASS" : "FAIL"} (d-files) ${singleHashedFile ? "PASS" : "FAIL"}`)
console.log(`  Final: ${pass ? "PASS" : "FAIL"}`)
console.log(`========================================\n`)

// ---- 8. Telemetry stderr capture ----
// The [media-guard:chat.message] lines get printed to stderr via MEDIA_GUARD_DEBUG

// Cleanup
try { spawnSync("rm", ["-rf", imgDir], { stdio: "pipe" }) } catch {}
