#!/usr/bin/env bun
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const configPath = join(testDir, "..", "..", "opencode.jsonc")
const pluginPath = join(testDir, "..", "media-guard.ts")
const benchmarkPath = join(testDir, "benchmark_local_vision.mjs")
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
for (const expected of ["verifiedEvidence", "visionCandidateEnabled === true && evidenceVerified", "ocrCorrection === true && evidenceVerified", 'artifact?.decision === "computed-from-validated-records"', 'gates?.manifest_identity === true', 'gates?.provenance === true']) {
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

const rollbackConfig = config
  .replace('"visionCandidateEnabled": false', '"visionCandidateEnabled": true')
  .replace('"evidenceSha256": ""', '"evidenceSha256": "' + "0".repeat(64) + '"')
if (!rollbackConfig.includes('"visionCandidateEnabled": true')) throw new Error("rollback drill setup failed")
if (!rollbackConfig.includes('"evidenceSha256": "' + "0".repeat(64) + '"')) throw new Error("rollback drill evidence setup failed")
if (!config.includes('"visionCandidateEnabled": false') || !config.includes('"ocrCorrection": false')) throw new Error("candidate is not disabled in current config")

console.log("PASS media-guard configuration and benchmark invocation")
