#!/usr/bin/env bun
import { $ } from "bun"
import { createHash, generateKeyPairSync, sign } from "node:crypto"
import { join } from "node:path"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"

const root = mkdtempSync(join(tmpdir(), "media-guard-evidence-"))
const { privateKey, publicKey } = generateKeyPairSync("ed25519")
const publicKeyDer = publicKey.export({ type: "spki", format: "der" }).toString("base64")
const canonical = value => value === null || typeof value !== "object" ? JSON.stringify(value) : Array.isArray(value) ? `[${value.map(canonical).join(",")}]` : `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`
const configSha256 = "d".repeat(64)
const attest = artifact => {
  const unsigned = { ...artifact }
  const attestation = { schema_version: 1, algorithm: "Ed25519", key_id: "test-key", issued_at: new Date(Date.now() - 1000).toISOString(), expires_at: new Date(Date.now() + 3600000).toISOString(), candidate_model: artifact.candidate_model, baseline_model: artifact.baseline_model, evidence_sha256: createHash("sha256").update(canonical(unsigned)).digest("hex"), config_sha256: configSha256, scorer_sha256: createHash("sha256").update(scorerBytes).digest("hex"), capabilities: ["visionCandidate", "stickyNotes"] }
  attestation.signature = sign(null, Buffer.from(canonical(attestation)), privateKey).toString("base64")
  return JSON.stringify({ ...unsigned, attestation })
}
const pluginPath = join(import.meta.dir, "..", "media-guard.ts")
const fixture = { path: "fixture.png", sha256: "a".repeat(64) }
const oracleBytes = JSON.stringify({ notes: [{ text: "ok", box: [0, 0, 1, 1] }] })
const oracle = { path: "oracle.json", sha256: createHash("sha256").update(oracleBytes).digest("hex") }
const ui = { schema_version: 1, synthetic_text: true, no_image_file_parts: true, local_transport: true, remote_fallback: false }
const uiBytes = JSON.stringify(ui)
const uiBinding = { path: "ui.json", sha256: createHash("sha256").update(uiBytes).digest("hex") }
const holdoutFixture = { path: "holdout-fixture.png", sha256: "c".repeat(64) }
const holdoutOracleBytes = JSON.stringify({ notes: [{ text: "holdout", box: [0, 0, 1, 1] }] })
const holdoutOracle = { path: "holdout-oracle.json", sha256: createHash("sha256").update(holdoutOracleBytes).digest("hex") }
const manifest = { schema_version: 2, provenance: { authorship: "human", blinding: "holdout" }, holdout: ["holdout"], ui_assertions: uiBinding, corpus: [{ id: "sample", split: "benchmark", fixture, oracle }, { id: "holdout", split: "holdout", fixture: holdoutFixture, oracle: holdoutOracle }] }
const raw = JSON.stringify({ notes: [{ content: "ok", bbox: [0, 0, 1, 1] }] })
const record = { schema_version: 2, run_id: "run", role: "production", model: "extract.py:production", sample_id: "sample", split: "benchmark", fixture, oracle, raw_output: raw, raw_output_sha256: createHash("sha256").update(raw).digest("hex"), errors: [], timed_out: false, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: { total: 1, authoritative: { total: 1, exact_value_safety: true, span_safety: true }, ui_assertions: { pass: true } }, provenance: { production_enabled: false } }
const baselineRecord = { ...record, run_id: "baseline-run", role: "tesseract", model: "extract.py:tesseract" }
const appleRecord = { ...record, run_id: "apple-run", role: "apple", model: "extract.py:apple" }
const manifestBytes = JSON.stringify(manifest)
const resultsBytes = [record, { ...record, run_id: "production-run-2" }, baselineRecord, { ...baselineRecord, run_id: "baseline-run-2" }, appleRecord, { ...appleRecord, run_id: "apple-run-2" }].map(JSON.stringify).join("\n") + "\n"
const scorer = { schema_version: 1, scorer_version: "media-guard-score-v1", thresholds: { total: 0.8, non_regression: 0.95 }, semantics: "oracle.notes text tokens are case-folded Unicode words; every token must occur in serialized output; every output note must contain box or bbox array; total is mean(exact_value_safety, span_safety)" }
const scorerBytes = JSON.stringify(scorer)
const holdoutRaw = JSON.stringify({ notes: [{ content: "holdout", bbox: [0, 0, 1, 1] }] })
const holdoutRecord = { schema_version: 2, run_id: "holdout-run", role: "candidate", model: "extract.py:production", sample_id: "holdout", split: "holdout", fixture: holdoutFixture, oracle: holdoutOracle, raw_output: holdoutRaw, raw_output_sha256: createHash("sha256").update(holdoutRaw).digest("hex"), errors: [], timed_out: false, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: { pass: true, total: 1, exact_value_safety: true, span_safety: true, ui_assertions: { pass: true } } }
const holdoutBaselineRecord = { ...holdoutRecord, run_id: "holdout-baseline-run", role: "baseline", model: "extract.py:tesseract" }
const holdoutResultsBytes = [holdoutRecord, { ...holdoutRecord, run_id: "holdout-run-2" }, holdoutBaselineRecord, { ...holdoutBaselineRecord, run_id: "holdout-baseline-run-2" }].map(JSON.stringify).join("\n") + "\n"
const holdoutEvaluation = { schema_version: 2, frozen: true, split: "holdout", review: "independent", manifest_sha256: createHash("sha256").update(manifestBytes).digest("hex"), candidate_model: "extract.py:production", holdout_ids: ["holdout"], results: { path: "holdout-results.jsonl", sha256: createHash("sha256").update(holdoutResultsBytes).digest("hex") }, scorer: { path: "scorer.json", sha256: createHash("sha256").update(scorerBytes).digest("hex") }, metrics: { pass: true }, gates: { pass: true }, decision: "independent-held-out-review" }
const holdoutEvaluationBytes = JSON.stringify(holdoutEvaluation)
writeFileSync(join(root, "manifest.json"), manifestBytes)
writeFileSync(join(root, "oracle.json"), oracleBytes)
writeFileSync(join(root, "holdout-oracle.json"), holdoutOracleBytes)
writeFileSync(join(root, "ui.json"), uiBytes)
writeFileSync(join(root, "results.jsonl"), resultsBytes)
writeFileSync(join(root, "holdout-evaluation.json"), holdoutEvaluationBytes)
writeFileSync(join(root, "holdout-results.jsonl"), holdoutResultsBytes)
writeFileSync(join(root, "scorer.json"), scorerBytes)
const artifact = {
  schema_version: 2,
  manifest: { path: "manifest.json", sha256: createHash("sha256").update(manifestBytes).digest("hex") },
  results: { path: "results.jsonl", sha256: createHash("sha256").update(resultsBytes).digest("hex") },
  holdout_evaluation: { path: "holdout-evaluation.json", sha256: createHash("sha256").update(holdoutEvaluationBytes).digest("hex") },
  candidate_model: "extract.py:production", baseline_model: "extract.py:tesseract", holdout: ["holdout"],
  ui_assertions: uiBinding, remote_fallback: false,
  gates: { schema: true, manifest_identity: true, provenance: true, holdout: true, holdout_evaluation: true, reproducibility: true, remote_fallback: true, rss: true, safety: true, non_regression: true },
  comparison: { pass: true, threshold: 0.95 }, metrics: { tesseract: { records: 1 }, production: { records: 1 } }, decision: "computed-from-validated-records",
}
const good = attest(artifact)
writeFileSync(join(root, "good.json"), good)
writeFileSync(join(root, "bad.json"), JSON.stringify({ decision: "PASS" }))
const image = `data:image/png;base64,${Buffer.from("not-an-image").toString("base64")}`
const seen = []
const originalFetch = globalThis.fetch
globalThis.fetch = async (_url, init) => {
  seen.push(JSON.parse(init.body).model)
  return new Response(JSON.stringify({ message: { content: JSON.stringify({ content_type: "image", summary: "ok", key_metadata: {}, relevant_spans: [], full_text: "ok" }) } }), { status: 200 })
}
const { MediaGuardPlugin } = await import(pluginPath)
async function modelFor(evidencePath, evidenceSha256) {
  seen.length = 0
  const hooks = await MediaGuardPlugin({ $, client: {} }, { visionEnabled: true, visionModel: "extract.py:tesseract", visionCandidateModel: "extract.py:production", visionCandidateEnabled: true, evidencePath, evidenceSha256, configSha256, trustedAttestationKeys: { "test-key": publicKeyDer }, agentKinds: ["image"] })
  await hooks["chat.message"]({}, { parts: [{ type: "file", mime: "image/png", url: image, filename: "x.png" }] })
  return seen[0] ?? "extract.py:tesseract"
}
const badModel = await modelFor(join(root, "bad.json"), createHash("sha256").update(JSON.stringify({ decision: "PASS" })).digest("hex"))
if (badModel !== "extract.py:tesseract") throw new Error(`bare PASS accepted: ${badModel}`)
if (seen.length !== 0) throw new Error("rejected evidence made local /api/chat request")
const goodModel = await modelFor(join(root, "good.json"), createHash("sha256").update(good).digest("hex"))
if (goodModel !== "extract.py:production") throw new Error(`validated evidence rejected: ${goodModel}`)
const signedMutation = (name, mutate) => {
  const mutated = JSON.parse(good)
  mutate(mutated.attestation)
  const unsigned = { ...mutated }
  delete unsigned.attestation
  mutated.attestation.evidence_sha256 = createHash("sha256").update(canonical(unsigned)).digest("hex")
  delete mutated.attestation.signature
  mutated.attestation.signature = sign(null, Buffer.from(canonical(mutated.attestation)), privateKey).toString("base64")
  const bytes = JSON.stringify(mutated)
  const path = join(root, `${name}.json`)
  writeFileSync(path, bytes)
  return modelFor(path, createHash("sha256").update(bytes).digest("hex"))
}
if (await signedMutation("expired", attestation => { attestation.expires_at = new Date(Date.now() - 1).toISOString() }) !== "extract.py:tesseract") throw new Error("expired attestation accepted")
if (seen.length !== 0) throw new Error("expired attestation made local /api/chat request")
if (await signedMutation("wrong-key", attestation => { attestation.key_id = "unknown" }) !== "extract.py:tesseract") throw new Error("untrusted signer accepted")
if (seen.length !== 0) throw new Error("untrusted signer made local /api/chat request")
if (await signedMutation("no-sticky-capability", attestation => { attestation.capabilities = ["visionCandidate"] }) !== "extract.py:production") throw new Error("capability mutation changed unrelated candidate admission")
if (await signedMutation("no-candidate-capability", attestation => { attestation.capabilities = ["stickyNotes"] }) !== "extract.py:tesseract") throw new Error("candidate capability bypassed")
if (await signedMutation("wrong-config", attestation => { attestation.config_sha256 = "e".repeat(64) }) !== "extract.py:tesseract") throw new Error("wrong config hash accepted")
if (await signedMutation("wrong-scorer", attestation => { attestation.scorer_sha256 = "e".repeat(64) }) !== "extract.py:tesseract") throw new Error("wrong scorer hash accepted")
if (await signedMutation("chain-claim", attestation => { attestation.sequence = 1; attestation.previous_hash = null }) !== "extract.py:tesseract") throw new Error("unenforced chain claim accepted")
const holdoutMutation = (name, mutate) => {
  const records = holdoutResultsBytes.split("\n").filter(Boolean).map(JSON.parse)
  mutate(records)
  const bytes = records.map(JSON.stringify).join("\n") + "\n"
  const resultPath = `${name}-holdout-results.jsonl`
  writeFileSync(join(root, resultPath), bytes)
  const mutated = JSON.parse(good)
  const evaluation = JSON.parse(holdoutEvaluationBytes)
  evaluation.results = { path: resultPath, sha256: createHash("sha256").update(bytes).digest("hex") }
  const evaluationBytes = JSON.stringify(evaluation)
  const artifactPath = `${name}.json`
  mutated.holdout_evaluation = { path: `${name}-holdout-evaluation.json`, sha256: createHash("sha256").update(evaluationBytes).digest("hex") }
  writeFileSync(join(root, mutated.holdout_evaluation.path), evaluationBytes)
  const artifactBytes = JSON.stringify(mutated)
  writeFileSync(join(root, artifactPath), artifactBytes)
  return modelFor(join(root, artifactPath), createHash("sha256").update(artifactBytes).digest("hex"))
}
if (await holdoutMutation("missing", records => records.pop()) !== "extract.py:tesseract") throw new Error("missing holdout record accepted")
if (await holdoutMutation("failed", records => { records[0].errors = ["timeout"]; }) !== "extract.py:tesseract") throw new Error("failed holdout record accepted")
if (await holdoutMutation("altered", records => { records[0].scores.pass = false; }) !== "extract.py:tesseract") throw new Error("altered holdout record accepted")
if (await holdoutMutation("mismatched", records => { records[0].model = "other-candidate"; }) !== "extract.py:tesseract") throw new Error("mismatched holdout record accepted")
if (await holdoutMutation("disagreeing", records => { records[1].raw_output = '{"different":true}'; records[1].raw_output_sha256 = createHash("sha256").update(records[1].raw_output).digest("hex") }) !== "extract.py:tesseract") throw new Error("disagreeing holdout record accepted")
const mismatched = JSON.parse(good)
mismatched.candidate_model = "other-candidate"
const mismatchedBytes = JSON.stringify(mismatched)
writeFileSync(join(root, "mismatched.json"), mismatchedBytes)
const mismatchModel = await modelFor(join(root, "mismatched.json"), createHash("sha256").update(mismatchedBytes).digest("hex"))
if (mismatchModel !== "extract.py:tesseract") throw new Error(`configured identity mismatch accepted: ${mismatchModel}`)
const badRecordArtifact = JSON.parse(good)
const badRecord = { ...record, model: "other-candidate" }
const badResultsBytes = JSON.stringify(badRecord) + "\n" + JSON.stringify(baselineRecord) + "\n"
writeFileSync(join(root, "bad-records.jsonl"), badResultsBytes)
badRecordArtifact.results = { path: "bad-records.jsonl", sha256: createHash("sha256").update(badResultsBytes).digest("hex") }
const badRecordBytes = JSON.stringify(badRecordArtifact)
writeFileSync(join(root, "bad-record.json"), badRecordBytes)
const badRecordModel = await modelFor(join(root, "bad-record.json"), createHash("sha256").update(badRecordBytes).digest("hex"))
if (badRecordModel !== "extract.py:tesseract") throw new Error(`record model binding accepted: ${badRecordModel}`)
const disagreementRecords = resultsBytes.split("\n").filter(Boolean).map(JSON.parse)
disagreementRecords[1].raw_output = '{"different":true}'
disagreementRecords[1].raw_output_sha256 = createHash("sha256").update(disagreementRecords[1].raw_output).digest("hex")
const disagreementResultsBytes = disagreementRecords.map(JSON.stringify).join("\n") + "\n"
writeFileSync(join(root, "disagreement-results.jsonl"), disagreementResultsBytes)
const disagreementArtifact = JSON.parse(good)
disagreementArtifact.results = { path: "disagreement-results.jsonl", sha256: createHash("sha256").update(disagreementResultsBytes).digest("hex") }
const disagreementBytes = JSON.stringify(disagreementArtifact)
writeFileSync(join(root, "disagreement.json"), disagreementBytes)
const disagreementModel = await modelFor(join(root, "disagreement.json"), createHash("sha256").update(disagreementBytes).digest("hex"))
if (disagreementModel !== "extract.py:tesseract") throw new Error(`repeated output disagreement accepted: ${disagreementModel}`)
const duplicateManifest = { ...manifest, corpus: [{ id: "sample", split: "benchmark", fixture, oracle }, { id: "holdout", split: "holdout", fixture, oracle }] }
const duplicateManifestBytes = JSON.stringify(duplicateManifest)
writeFileSync(join(root, "duplicate-manifest.json"), duplicateManifestBytes)
const duplicateArtifact = { ...JSON.parse(good), manifest: { path: "duplicate-manifest.json", sha256: createHash("sha256").update(duplicateManifestBytes).digest("hex") } }
const duplicateArtifactBytes = JSON.stringify(duplicateArtifact)
writeFileSync(join(root, "duplicate.json"), duplicateArtifactBytes)
const duplicateModel = await modelFor(join(root, "duplicate.json"), createHash("sha256").update(duplicateArtifactBytes).digest("hex"))
if (duplicateModel !== "extract.py:tesseract") throw new Error(`benchmark/holdout duplicate fixture/oracle accepted: ${duplicateModel}`)
globalThis.fetch = originalFetch
console.log("PASS evidence rejection/acceptance: bare PASS, model mismatches, repeated disagreement, and benchmark/holdout duplicate hashes rejected; immutable gated artifact accepted")
