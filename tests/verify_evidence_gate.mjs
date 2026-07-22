#!/usr/bin/env bun
import { $ } from "bun"
import { createHash } from "node:crypto"
import { join } from "node:path"
import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"

const root = mkdtempSync(join(tmpdir(), "media-guard-evidence-"))
const pluginPath = join(import.meta.dir, "..", "media-guard.ts")
const fixture = { path: "fixture.png", sha256: "a".repeat(64) }
const oracle = { path: "oracle.json", sha256: "b".repeat(64) }
const ui = { schema_version: 1, synthetic_text: true, no_image_file_parts: true, local_transport: true, remote_fallback: false }
const uiBytes = JSON.stringify(ui)
const uiBinding = { path: "ui.json", sha256: createHash("sha256").update(uiBytes).digest("hex") }
const manifest = { schema_version: 2, provenance: { authorship: "human", blinding: "holdout" }, holdout: ["holdout"], ui_assertions: uiBinding, corpus: [{ id: "sample", split: "benchmark", fixture, oracle }] }
const raw = "{}"
const record = { schema_version: 2, run_id: "run", role: "production", model: "extract.py:production", sample_id: "sample", split: "benchmark", fixture, oracle, raw_output: raw, raw_output_sha256: createHash("sha256").update(raw).digest("hex"), errors: [], timed_out: false, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: { total: 1, authoritative: { total: 1, exact_value_safety: true, span_safety: true }, ui_assertions: { pass: true } }, provenance: { production_enabled: false } }
const baselineRecord = { ...record, run_id: "baseline-run", role: "tesseract", model: "extract.py:tesseract" }
const appleRecord = { ...record, run_id: "apple-run", role: "apple", model: "extract.py:apple" }
const manifestBytes = JSON.stringify(manifest)
const resultsBytes = JSON.stringify(record) + "\n" + JSON.stringify(baselineRecord) + "\n" + JSON.stringify(appleRecord) + "\n"
writeFileSync(join(root, "manifest.json"), manifestBytes)
writeFileSync(join(root, "ui.json"), uiBytes)
writeFileSync(join(root, "results.jsonl"), resultsBytes)
const artifact = {
  schema_version: 2,
  manifest: { path: "manifest.json", sha256: createHash("sha256").update(manifestBytes).digest("hex") },
  results: { path: "results.jsonl", sha256: createHash("sha256").update(resultsBytes).digest("hex") },
  candidate_model: "extract.py:production", baseline_model: "extract.py:tesseract", holdout: ["holdout"],
  ui_assertions: uiBinding, remote_fallback: false,
  gates: { schema: true, manifest_identity: true, provenance: true, holdout: true, remote_fallback: true, rss: true, safety: true, non_regression: true },
  comparison: { pass: true, threshold: 0.95 }, metrics: { tesseract: { records: 1 }, production: { records: 1 } }, decision: "computed-from-validated-records",
}
const good = JSON.stringify(artifact)
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
  const hooks = await MediaGuardPlugin({ $, client: {} }, { visionEnabled: true, visionModel: "extract.py:tesseract", visionCandidateModel: "extract.py:production", visionCandidateEnabled: true, evidencePath, evidenceSha256, agentKinds: ["image"] })
  await hooks["chat.message"]({}, { parts: [{ type: "file", mime: "image/png", url: image, filename: "x.png" }] })
  return seen[0]
}
const badModel = await modelFor(join(root, "bad.json"), createHash("sha256").update(JSON.stringify({ decision: "PASS" })).digest("hex"))
if (badModel !== "extract.py:tesseract") throw new Error(`bare PASS accepted: ${badModel}`)
const goodModel = await modelFor(join(root, "good.json"), createHash("sha256").update(good).digest("hex"))
if (goodModel !== "extract.py:production") throw new Error(`validated evidence rejected: ${goodModel}`)
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
const duplicateManifest = { ...manifest, corpus: [{ id: "sample", split: "benchmark", fixture, oracle }, { id: "holdout", split: "holdout", fixture, oracle }] }
const duplicateManifestBytes = JSON.stringify(duplicateManifest)
writeFileSync(join(root, "duplicate-manifest.json"), duplicateManifestBytes)
const duplicateArtifact = { ...JSON.parse(good), manifest: { path: "duplicate-manifest.json", sha256: createHash("sha256").update(duplicateManifestBytes).digest("hex") } }
const duplicateArtifactBytes = JSON.stringify(duplicateArtifact)
writeFileSync(join(root, "duplicate.json"), duplicateArtifactBytes)
const duplicateModel = await modelFor(join(root, "duplicate.json"), createHash("sha256").update(duplicateArtifactBytes).digest("hex"))
if (duplicateModel !== "extract.py:tesseract") throw new Error(`benchmark/holdout duplicate fixture/oracle accepted: ${duplicateModel}`)
globalThis.fetch = originalFetch
console.log("PASS evidence rejection/acceptance: bare PASS, model mismatches, and benchmark/holdout duplicate hashes rejected; immutable gated artifact accepted")
