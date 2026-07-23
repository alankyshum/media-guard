#!/usr/bin/env bun
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const configPath = process.env.MEDIA_GUARD_CONFIG || join(testDir, "..", "..", "..", "config", "opencode", "opencode.jsonc")
const pluginPath = join(testDir, "..", "media-guard.ts")
const benchmarkPath = join(testDir, "benchmark_local_vision.mjs")
const rollbackPath = join(testDir, "verify_rollback.mjs")
const videoMissPath = join(testDir, "verify_video_local_miss.mjs")
const config = readFileSync(configPath, "utf8")
const plugin = readFileSync(pluginPath, "utf8")
const benchmark = readFileSync(benchmarkPath, "utf8")
const selectedModel = process.env.MEDIA_GUARD_SELECTED_MODEL || "qwen2.5vl:7b"
const baselineModel = process.env.MEDIA_GUARD_BASELINE_MODEL || "gemma4:12b"

for (const expected of [
  '"visionEnabled": true',
  `"visionModel": "${baselineModel}"`,
  `"visionCandidateModel": "${selectedModel}"`,
  '"visionCandidateEnabled": false',
  '"ocrCorrection": false',
  '"evidenceSha256": ""',
  '"configSha256": ""',
  '"visionBaseUrl": "http://127.0.0.1:11434"',
  '"visionTimeoutSec": 120',
  '"visionNumCtx": 16384',
  '"agentKinds": ["image", "video"]',
]) {
  if (!config.includes(expected)) throw new Error(`config missing ${expected}`)
}

if (!plugin.includes(`opts.visionModel ?? "${baselineModel}"`)) {
  throw new Error("plugin fallback model drifted from baseline configuration")
}
for (const expected of ["verifiedEvidence", "candidateRequested && evidenceVerified", "ocrCorrection === true && evidenceVerified", "forceDeterministicOnRejectedCandidate", 'artifact?.decision === "computed-from-validated-records"', 'gates?.manifest_identity === true', 'gates?.provenance === true', 'gates?.reproducibility === true', 'gates?.holdout_evaluation === true', 'artifact.holdout_evaluation']) {
  if (!plugin.includes(expected)) throw new Error(`evidence gate missing ${expected}`)
}
for (const expected of ['model, messages:', 'options: { temperature: 0, num_ctx: 16384 }', '/api/chat', 'remote_fallback: false']) {
  if (!benchmark.includes(expected)) throw new Error(`benchmark model invocation missing ${expected}`)
}
const currentModel = process.env.MEDIA_GUARD_CURRENT_MODEL || selectedModel
for (const model of [selectedModel, currentModel, baselineModel]) {
  if (!benchmark.includes(model)) throw new Error(`benchmark missing parameterized model: ${model}`)
}
if (benchmark.includes('opencode", args') || benchmark.includes('runAgent')) throw new Error("benchmark must not use remote fallback")
for (const expected of [
  'const runExtract = async',
  'deadline = Infinity',
  'remainingSec',
  'Math.min(timeoutSec, remainingSec)',
  'fallback to deterministic local extraction',
  'forceDeterministicOnRejectedCandidate',
  'rejectedCandidate || matches.length > batchThreshold',
]) {
  if (!plugin.includes(expected)) throw new Error(`deadline-aware fallback missing ${expected}`)
}
if (!/runExtract\([^\n]+,\s*fileDeadline\)/.test(plugin)) {
  throw new Error("deadline-aware fallback does not pass per-file deadline")
}
if (plugin.includes('opencode", args') || plugin.includes('"-f"')) throw new Error("media guard must not invoke opencode file fallback")

const result = spawnSync("bun", [
  benchmarkPath,
  "--help",
], { encoding: "utf8" })
if (result.status !== 0 || !result.stdout.includes("--model <tag>")) {
  throw new Error(`benchmark invocation failed:\n${result.stderr}`)
}

const rollback = spawnSync("bun", [rollbackPath], { encoding: "utf8" })
if (rollback.status !== 0) throw new Error(`rollback verification failed:\n${rollback.stdout}\n${rollback.stderr}`)
if (!rollback.stdout.includes('"opposite_mode_key_miss":true') ||
     !rollback.stdout.includes('"output_matches_independent_baseline":true') ||
     !rollback.stdout.includes('"evidence_scope":"gate-mechanics-only; synthetic evidence is not quality proof"') ||
    !rollback.stdout.includes('"enabled_candidate":1') ||
    !rollback.stdout.includes('"disabled_candidate":0')) {
  throw new Error(`rollback verification missing required trace:\n${rollback.stdout}`)
}
console.log(`ROLLBACK_TRACE ${rollback.stdout.trim()}`)

const videoMiss = spawnSync("bun", [videoMissPath], { encoding: "utf8" })
if (videoMiss.status !== 0) throw new Error(`video miss regression failed:\n${videoMiss.stdout}\n${videoMiss.stderr}`)
console.log(`VIDEO_TRACE ${videoMiss.stdout.trim()}`)

const rollbackConfig = config
  .replace('"visionCandidateEnabled": false', '"visionCandidateEnabled": true')
  .replace('"evidenceSha256": ""', '"evidenceSha256": "' + "0".repeat(64) + '"')
if (!rollbackConfig.includes('"visionCandidateEnabled": true')) throw new Error("rollback drill setup failed")
if (!rollbackConfig.includes('"evidenceSha256": "' + "0".repeat(64) + '"')) throw new Error("rollback drill evidence setup failed")
if (!config.includes('"visionCandidateEnabled": false') || !config.includes('"ocrCorrection": false')) throw new Error("candidate is not disabled in current config")

console.log("PASS media-guard configuration and benchmark invocation")
