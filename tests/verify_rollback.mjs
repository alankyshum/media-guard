#!/usr/bin/env bun
import { $ } from "bun"
import { createServer } from "node:http"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { createHash, generateKeyPairSync, sign } from "node:crypto"
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const pluginPath = join(testDir, "..", "media-guard.ts")
const genScript = join(testDir, "gen_images.py")
const workDir = mkdtempSync(join(tmpdir(), "media-guard-rollback-"))
const cacheDir = join(workDir, "cache")
const imageDir = join(workDir, "images")
const { privateKey, publicKey } = generateKeyPairSync("ed25519")
const publicKeyDer = publicKey.export({ type: "spki", format: "der" }).toString("base64")
const canonical = value => value === null || typeof value !== "object" ? JSON.stringify(value) : Array.isArray(value) ? `[${value.map(canonical).join(",")}]` : `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`

const previousEnv = { ...process.env }
const requests = []
const server = createServer((request, response) => {
  let body = ""
  request.on("data", chunk => { body += chunk })
  request.on("end", () => {
    requests.push({ url: request.url || "", model: body ? JSON.parse(body).model : null })
    response.writeHead(200, { "content-type": "application/json" })
    response.end(JSON.stringify({ message: { content: JSON.stringify({
      content_type: "image",
      summary: "candidate digest",
      key_metadata: { model: "sticky-candidate-not-enabled" },
      relevant_spans: [],
      full_text: "candidate digest"
    }) } }))
  })
})

const restore = () => {
  for (const key of Object.keys(process.env)) {
    if (!(key in previousEnv)) delete process.env[key]
  }
  Object.assign(process.env, previousEnv)
  try { server.close() } catch {}
  rmSync(workDir, { recursive: true, force: true })
}

try {
  const generated = spawnSync("python3", [genScript, imageDir, "1"], { encoding: "utf8" })
  if (generated.status !== 0) throw new Error(`fixture generation failed:\n${generated.stderr}`)
  const imageName = readdirSync(imageDir).find(name => name.endsWith(".png"))
  if (!imageName) throw new Error("rollback fixture missing")
  const imagePath = join(imageDir, imageName)
  const dataUrl = `data:image/png;base64,${readFileSync(imagePath).toString("base64")}`

  const uiBytes = JSON.stringify({ schema_version: 1, synthetic_text: true, no_image_file_parts: true, local_transport: true, remote_fallback: false })
  const oracleBytes = JSON.stringify({ notes: [{ text: "ok", box: [0, 0, 1, 1] }] })
  const oracleHash = createHash("sha256").update(oracleBytes).digest("hex")
  const holdoutOracleBytes = JSON.stringify({ notes: [{ text: "holdout", box: [0, 0, 1, 1] }] })
  const holdoutOracleHash = createHash("sha256").update(holdoutOracleBytes).digest("hex")
  const recordRaw = JSON.stringify({ notes: [{ content: "ok", bbox: [0, 0, 1, 1] }] })
  const recordRawHash = createHash("sha256").update(recordRaw).digest("hex")
  const manifestBytes = JSON.stringify({
    schema_version: 2,
    provenance: { authorship: "human", blinding: "holdout" },
    holdout: ["holdout"],
    ui_assertions: { path: "ui.json", sha256: createHash("sha256").update(uiBytes).digest("hex") },
    corpus: [
      { id: "sample", split: "benchmark", fixture: { path: "fixture.png", sha256: "a".repeat(64) }, oracle: { path: "oracle.json", sha256: oracleHash } },
      { id: "holdout", split: "holdout", fixture: { path: "holdout-fixture.png", sha256: "c".repeat(64) }, oracle: { path: "holdout-oracle.json", sha256: holdoutOracleHash } },
    ]
  })
  const recordBase = {
    schema_version: 2, split: "benchmark", sample_id: "sample",
    fixture: { path: "fixture.png", sha256: "a".repeat(64) },
    oracle: { path: "oracle.json", sha256: oracleHash }, raw_output: recordRaw,
    raw_output_sha256: recordRawHash, errors: [], timed_out: false,
    rss: { gate_pass: true }, scores: { total: 1, exact_value_safety: true, span_safety: true, authoritative: { total: 1, exact_value_safety: true, span_safety: true }, ui_assertions: { pass: true } },
    provenance: { production_enabled: false }
  }
  const resultsBytes = [
    { ...recordBase, role: "production", model: "sticky-candidate-not-enabled" },
    { ...recordBase, run_id: "production-2", role: "production", model: "sticky-candidate-not-enabled" },
    { ...recordBase, role: "tesseract", model: "gemma4:12b" },
    { ...recordBase, run_id: "tesseract-2", role: "tesseract", model: "gemma4:12b" },
    { ...recordBase, role: "apple", model: "extract.py:apple" },
    { ...recordBase, run_id: "apple-2", role: "apple", model: "extract.py:apple" },
  ].map(record => JSON.stringify(record)).join("\n") + "\n"
  const holdoutRecordBase = {
    schema_version: 2, split: "holdout", sample_id: "holdout",
    fixture: { path: "holdout-fixture.png", sha256: "c".repeat(64) },
    oracle: { path: "holdout-oracle.json", sha256: holdoutOracleHash },
    raw_output: JSON.stringify({ notes: [{ content: "holdout", bbox: [0, 0, 1, 1] }] }), raw_output_sha256: createHash("sha256").update(JSON.stringify({ notes: [{ content: "holdout", bbox: [0, 0, 1, 1] }] })).digest("hex"),
    errors: [], timed_out: false, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true },
    scores: { pass: true, total: 1, exact_value_safety: true, span_safety: true, ui_assertions: { pass: true } },
  }
  const holdoutResultsBytes = [
    { ...holdoutRecordBase, run_id: "holdout-candidate-1", role: "candidate", model: "sticky-candidate-not-enabled" },
    { ...holdoutRecordBase, run_id: "holdout-candidate-2", role: "candidate", model: "sticky-candidate-not-enabled" },
    { ...holdoutRecordBase, run_id: "holdout-baseline-1", role: "baseline", model: "gemma4:12b" },
    { ...holdoutRecordBase, run_id: "holdout-baseline-2", role: "baseline", model: "gemma4:12b" },
  ].map(record => JSON.stringify(record)).join("\n") + "\n"
  const scorerBytes = JSON.stringify({ schema_version: 1, scorer_version: "media-guard-score-v1", thresholds: { total: 0.8, non_regression: 0.95 }, semantics: "oracle.notes text tokens are case-folded Unicode words; every token must occur in serialized output; every output note must contain box or bbox array; total is mean(exact_value_safety, span_safety)" })
  const holdoutEvaluationBytes = JSON.stringify({
    schema_version: 2, frozen: true, split: "holdout", review: "independent",
    manifest_sha256: createHash("sha256").update(manifestBytes).digest("hex"),
    candidate_model: "sticky-candidate-not-enabled", holdout_ids: ["holdout"],
    results: { path: "holdout-results.jsonl", sha256: createHash("sha256").update(holdoutResultsBytes).digest("hex") },
    scorer: { path: "scorer.json", sha256: createHash("sha256").update(scorerBytes).digest("hex") },
    metrics: { pass: true }, gates: { pass: true }, decision: "independent-held-out-review",
  })
  const unsignedArtifact = {
    schema_version: 2,
    manifest: { path: "manifest.json", sha256: createHash("sha256").update(manifestBytes).digest("hex") },
    results: { path: "results.jsonl", sha256: createHash("sha256").update(resultsBytes).digest("hex") },
    holdout_evaluation: { path: "holdout-evaluation.json", sha256: createHash("sha256").update(holdoutEvaluationBytes).digest("hex") },
    candidate_model: "sticky-candidate-not-enabled", baseline_model: "gemma4:12b",
    holdout: ["holdout"],
    ui_assertions: { path: "ui.json", sha256: createHash("sha256").update(uiBytes).digest("hex") },
    remote_fallback: false,
    gates: { schema: true, manifest_identity: true, provenance: true, holdout: true, holdout_evaluation: true, reproducibility: true, remote_fallback: true, rss: true, safety: true, non_regression: true },
    comparison: { pass: true, threshold: 0.95 }, metrics: { tesseract: { records: 1 }, production: { records: 1 } },
    decision: "computed-from-validated-records"
  }
  const configSha256 = "d".repeat(64)
  const attestation = { schema_version: 1, algorithm: "Ed25519", key_id: "rollback-key", issued_at: new Date(Date.now() - 1000).toISOString(), expires_at: new Date(Date.now() + 3600000).toISOString(), candidate_model: unsignedArtifact.candidate_model, baseline_model: unsignedArtifact.baseline_model, evidence_sha256: createHash("sha256").update(canonical(unsignedArtifact)).digest("hex"), config_sha256: configSha256, scorer_sha256: createHash("sha256").update(scorerBytes).digest("hex"), capabilities: ["visionCandidate", "stickyNotes"] }
  attestation.signature = sign(null, Buffer.from(canonical(attestation)), privateKey).toString("base64")
  const artifact = { ...unsignedArtifact, attestation }
  const evidencePath = join(workDir, "evidence.json")
  for (const [name, contents] of [["manifest.json", manifestBytes], ["oracle.json", oracleBytes], ["holdout-oracle.json", holdoutOracleBytes], ["ui.json", uiBytes], ["results.jsonl", resultsBytes], ["holdout-evaluation.json", holdoutEvaluationBytes], ["holdout-results.jsonl", holdoutResultsBytes], ["scorer.json", scorerBytes]]) {
    writeFileSync(join(workDir, name), contents)
  }
  writeFileSync(evidencePath, JSON.stringify(artifact))
  const evidenceSha256 = createHash("sha256").update(readFileSync(evidencePath)).digest("hex")
  const evidenceCheck = spawnSync("bun", [join(testDir, "verify_evidence_gate.mjs")], { encoding: "utf8" })
  if (evidenceCheck.status !== 0) throw new Error(`evidence gate self-test failed:\n${evidenceCheck.stdout}\n${evidenceCheck.stderr}`)

  process.env.MEDIA_CACHE_DIR = cacheDir
  process.env.OCR_STICKY_RESIDENT_ENABLED = "0"
  process.env.OCR_VLM_ENABLED = "0"
  process.env.GRANITE_DOCLING_ENABLED = "0"
  process.env.OCR_CORRECTION_ENABLED = "0"

  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  const address = server.address()
  if (typeof address !== "object" || !address) throw new Error("rollback request server failed to start")

  const cacheProbe = String.raw`
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("extract", Path(sys.argv[1]))
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)
source = sys.argv[2]
with open(source, "w") as f: f.write("same bytes")
os.environ["OCR_STICKY_RESIDENT_ENABLED"] = "0"
disabled_key = extract.cache_key(source, "image", "base", 60000)
extract.cache_put(disabled_key, {"mode": "disabled"})
os.environ["OCR_STICKY_RESIDENT_ENABLED"] = "1"
enabled_key = extract.cache_key(source, "image", "base", 60000)
enabled_miss = extract.cache_get(enabled_key) is None
os.environ["OCR_STICKY_RESIDENT_ENABLED"] = "0"
rollback_hit = extract.cache_get(disabled_key) == {"mode": "disabled"}
print(json.dumps({"disabled_key": disabled_key, "enabled_key": enabled_key, "enabled_miss": enabled_miss, "rollback_hit": rollback_hit}))
`
  const probeSource = join(workDir, "cache-probe.png")
  const probe = spawnSync("python3", ["-c", cacheProbe, join(testDir, "..", "media", "extract.py"), probeSource], {
    encoding: "utf8",
    env: { ...process.env, MEDIA_CACHE_DIR: cacheDir },
  })
  if (probe.status !== 0) throw new Error(`cache transition probe failed:\n${probe.stderr}`)
  const cacheTrace = JSON.parse(probe.stdout)
  if (cacheTrace.disabled_key === cacheTrace.enabled_key || !cacheTrace.enabled_miss || !cacheTrace.rollback_hit) {
    throw new Error(`opposite-mode cache transition failed: ${probe.stdout}`)
  }

  const { MediaGuardPlugin } = await import(pluginPath)
  const baseOpts = {
    visionEnabled: false,
    visionCandidateModel: "sticky-candidate-not-enabled",
    evidencePath,
    evidenceSha256,
    configSha256,
    trustedAttestationKeys: { "rollback-key": publicKeyDer },
    agentKinds: ["image"],
    extractorCache: false,
    timeoutSec: 30,
    transformBudgetSec: 30,
  }
  const enabledSimulation = await MediaGuardPlugin({ $, client: { app: { log: async () => {} } } }, {
    ...baseOpts,
    visionEnabled: true,
    visionCandidateEnabled: true,
    visionBaseUrl: `http://127.0.0.1:${address.port}`,
  })
  if (!evidenceSha256 || evidenceSha256.length !== 64) throw new Error("rollback evidence hash was not generated")
  const disabledRollback = await MediaGuardPlugin({ $, client: { app: { log: async () => {} } } }, {
    ...baseOpts,
    visionCandidateEnabled: false,
    visionBaseUrl: `http://127.0.0.1:${address.port}`,
  })

  const output = () => ({ parts: [
    { type: "text", text: "read this" },
    { type: "file", mime: "image/png", url: dataUrl, filename: "rollback.png" },
  ] })
  const enabledOutput = output()
  const disabledOutput = output()
  await enabledSimulation["chat.message"]({}, enabledOutput)
  await disabledRollback["chat.message"]({}, disabledOutput)
  const enabledText = enabledOutput.parts[1]?.text
  const disabledText = disabledOutput.parts[1]?.text
  if (enabledOutput.parts.some(part => part.type === "file") || disabledOutput.parts.some(part => part.type === "file")) {
    throw new Error("rollback left a resident file part")
  }
  const enabledRequests = requests.splice(0)
  if (!enabledRequests.some(request => request.url === "/api/chat" && request.model === "sticky-candidate-not-enabled")) {
    throw new Error(`valid evidence did not reach candidate endpoint: ${JSON.stringify({ requests: enabledRequests, text: enabledText })}`)
  }
  if (!enabledText?.includes("candidate digest")) throw new Error("enabled candidate output missing")
  const disabledRequests = requests.splice(0)
  if (disabledRequests.length !== 0) throw new Error(`disabled rollback made candidate traffic: ${JSON.stringify(disabledRequests)}`)
  if (!disabledText?.includes("MEDIAGUARD OCR TOKEN 0001")) throw new Error("rollback baseline OCR output missing")
  if (disabledText.includes("local vision") || disabledText.includes("sticky-candidate-not-enabled")) {
    throw new Error("rollback output used candidate/vision path")
  }

  const baselineProbe = spawnSync("python3", [join(testDir, "..", "media", "extract.py"), imagePath, "--mime", "image/png", "--max-chars", "60000", "--timeout", "30", "--model", "base", "--ocr-engine", "tesseract", "--no-cache"], { encoding: "utf8", env: { ...process.env, MEDIA_CACHE_DIR: cacheDir } })
  if (baselineProbe.status !== 0) throw new Error(`independent baseline probe failed:\n${baselineProbe.stderr}`)
  const independentBaseline = JSON.parse(baselineProbe.stdout)
  if (independentBaseline.status !== "ok" || typeof independentBaseline.text !== "string") throw new Error("independent baseline unavailable")
  const normalize = value => String(value).replaceAll(imagePath, "<fixture>").replaceAll("rollback.png", "<fixture-name>").trim()
  const rollbackTextMatch = String(disabledText).match(/BEGIN AUTHORITATIVE ORIGINAL IMAGE OCR\/TEXT -----\n([\s\S]*?)\n----- END AUTHORITATIVE ORIGINAL IMAGE OCR\/TEXT/)
  if (!rollbackTextMatch) throw new Error("rollback authoritative output missing")
  const normalizedRollback = normalize(rollbackTextMatch[1])
  const normalizedBaseline = normalize(independentBaseline.text)
  const rollbackHash = createHash("sha256").update(normalizedRollback).digest("hex")
  const baselineHash = createHash("sha256").update(normalizedBaseline).digest("hex")
  if (rollbackHash !== baselineHash || normalizedRollback !== normalizedBaseline) throw new Error(`rollback output diverged from independent baseline: ${JSON.stringify({ rollbackHash, baselineHash })}`)

  console.log(JSON.stringify({
    transition: "enabled-simulation -> disabled-rollback",
    cache: {
      opposite_mode_key_miss: cacheTrace.enabled_miss,
      rollback_mode_cache_hit: cacheTrace.rollback_hit,
    },
    output: {
      deterministic_baseline: { output_matches_independent_baseline: true, normalized_output_sha256: rollbackHash, independent_baseline_sha256: baselineHash },
      enabled_candidate_digest: "simulation-only",
      evidence_scope: "gate-mechanics-only; synthetic evidence is not quality proof",
      token: "MEDIAGUARD OCR TOKEN 0001",
      file_parts_after_rollback: 0,
    },
      requests: { enabled_candidate: enabledRequests.length, disabled_candidate: disabledRequests.length, total: enabledRequests.length + disabledRequests.length },
  }))
} finally {
  restore()
}
