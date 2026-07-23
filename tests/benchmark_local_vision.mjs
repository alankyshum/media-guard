#!/usr/bin/env bun
import { createHash, randomUUID } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { join, resolve } from "node:path"
import { tmpdir } from "node:os"
import { spawn } from "node:child_process"

const DEFAULT_OUT = join(tmpdir(), "opencode-media-guard-benchmark")
const OLLAMA_URL = "http://127.0.0.1:11434"
const RSS_LIMIT_BYTES = 40 * 1024 * 1024 * 1024
const SCHEMA_VERSION = 2
const NON_REGRESSION_THRESHOLD = 0.95
const TOTAL_SCORE_THRESHOLD = 0.8

function help() {
  console.log(`Usage: bun benchmark_local_vision.mjs [options]

Options:
  --manifest <path>       External corpus manifest (required)
  --model <tag>            Candidate model (alias: --candidate-model)
  --baseline-model <tag>  Baseline model
  --repetitions <n>        One cold run plus n warm runs (default: 2)
  --timeout-sec <n>        Per-request timeout (default: 120)
  --sample-ms <n>          RSS sampling interval (default: 250)
  --extract <path>         extract.py path
  --extract-pipeline       Run extract.py Apple/Tesseract/production engines
   --out <dir>              Artifact directory (must not already exist)
  --holdout-evaluation <path> Frozen independent holdout evaluation artifact
  --check                  Validate manifest, files, hashes, and model inventory
  --dry-run                Validate inputs and print immutable run plan; no inference
  --self-test              Run synthetic valid/rejection tests; no Ollama required
  --validate <path>        Validate JSONL benchmark artifact
  --help                   Show this help`)
}

function args(argv) {
  const out = { repetitions: 2, timeoutSec: 120, sampleMs: 250, out: DEFAULT_OUT, expectedModel: "qwen2.5vl:7b", baselineModel: "gemma4:12b", extract: resolve(import.meta.dirname, "../media/extract.py") }
  const keys = { manifest: "manifest", model: "model", "candidate-model": "model", "expected-model": "expectedModel", "current-model": "model", "baseline-model": "baselineModel", repetitions: "repetitions", "timeout-sec": "timeoutSec", "sample-ms": "sampleMs", out: "out", extract: "extract", "holdout-evaluation": "holdoutEvaluation" }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === "--help" || a === "-h") return { help: true }
    if (a === "--dry-run") { out.dryRun = true; continue }
    if (a === "--check") { out.check = true; continue }
    if (a === "--self-test") { out.selfTest = true; continue }
    if (a === "--extract-pipeline") { out.extractPipeline = true; continue }
    if (a === "--validate") { out.validate = argv[++i]; continue }
    if (!a.startsWith("--") || !keys[a.slice(2)]) throw new Error(`Unknown option: ${a}`)
    out[keys[a.slice(2)]] = argv[++i]
  }
  out.repetitions = Number(out.repetitions); out.timeoutSec = Number(out.timeoutSec); out.sampleMs = Number(out.sampleMs)
  if (![out.repetitions, out.timeoutSec, out.sampleMs].every(Number.isFinite) || out.repetitions < 0 || out.timeoutSec <= 0 || out.sampleMs <= 0) throw new Error("Invalid numeric option")
  out.model ??= out.expectedModel
  return out
}

function fail(message) { throw new Error(`INVALID: ${message}`) }
function sha256(bytes) { return createHash("sha256").update(bytes).digest("hex") }
function normalize(s) { return String(s ?? "").replace(/\s+/g, " ").trim().toLowerCase() }
function ocrQuality(text, confidence = 0) {
  const value = String(text ?? "").trim()
  if (!value) return -1
  const printable = [...value].filter(ch => ch === "\n" || ch === "\t" || ch.charCodeAt(0) >= 32).length / value.length
  const words = value.split(/\s+/).filter(Boolean)
  const alphaNum = [...value].filter(ch => /[\p{L}\p{N}]/u.test(ch)).length / value.length
  const lines = value.split(/\r?\n/).filter(line => line.trim())
  return Math.min(value.length, 4000) / 4000 * .25 + printable * .25 + alphaNum * .2 + Math.min(words.length, 120) / 120 * .15 + Math.min(lines.length, 30) / 30 * .05 + normalizeOcrConfidence(confidence) / 100 * .1
}
function normalizeOcrConfidence(value) {
  const confidence = Number(value)
  if (!Number.isFinite(confidence)) return 0
  return Math.min(Math.max(confidence, 0), 100)
}
function similarity(a, b) {
  a = normalize(a); b = normalize(b)
  const prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i++) { let left = i - 1, diagonal = i - 1; for (let j = 1; j <= b.length; j++) { const above = prev[j]; prev[j] = a[i - 1] === b[j - 1] ? diagonal : 1 + Math.min(above, left, diagonal); left = prev[j]; diagonal = above } }
  return Math.max(a.length, b.length) ? 1 - prev[b.length] / Math.max(a.length, b.length) : 1
}

function readJson(path) { try { return JSON.parse(readFileSync(path, "utf8")) } catch (e) { fail(`${path}: ${e.message}`) } }
function hashFile(path, expected, label) { if (!existsSync(path)) fail(`${label} missing: ${path}`); const actual = sha256(readFileSync(path)); if (actual !== expected) fail(`${label} SHA-256 mismatch: ${actual}`); return actual }

function validateOracle(oracle, label = "oracle") {
  if (!oracle || oracle.schema_version !== 1 || oracle.authorship !== "human" || oracle.provenance !== "blinded-human") fail(`${label} must be a blinded human oracle; model-authored or missing provenance rejected`)
  if (oracle.model_authored === true || oracle.generated_by_model) fail(`${label} is model-authored`)
  if (!oracle.verbatim_body_text || !Array.isArray(oracle.required_tokens) || !oracle.metadata || !Array.isArray(oracle.relevant_spans)) fail(`${label} missing exact text, tokens, metadata, or spans`)
  if (oracle.relevant_spans.some(s => !s || typeof s.quote !== "string" || !s.quote)) fail(`${label} contains invalid span`)
  return true
}

function validateManifest(manifest, base = process.cwd()) {
  if (!manifest || manifest.schema_version !== SCHEMA_VERSION) fail("manifest schema_version must be 2")
  if (manifest.provenance?.authorship !== "human" || manifest.provenance?.blinding !== "holdout") fail("manifest provenance must be blinded human")
  if (manifest.provenance?.model_authored === true || manifest.provenance?.oracle_source === "model") fail("manifest has model-authored provenance")
  if (!manifest.ui_assertions?.path || !/^[a-f0-9]{64}$/.test(manifest.ui_assertions.sha256)) fail("manifest UI assertions must be hash-bound")
  const uiPath = resolve(base, manifest.ui_assertions.path)
  hashFile(uiPath, manifest.ui_assertions.sha256, "UI assertions")
  const ui = readJson(uiPath)
  if (ui.schema_version !== 1 || typeof ui.synthetic_text !== "boolean" || typeof ui.no_image_file_parts !== "boolean" || typeof ui.local_transport !== "boolean" || typeof ui.remote_fallback !== "boolean" || ui.remote_fallback) fail("invalid UI assertions or remote fallback detected")
  if (!Array.isArray(manifest.corpus) || !manifest.corpus.length) fail("manifest corpus is empty")
  if (!Array.isArray(manifest.holdout) || !manifest.holdout.length) fail("manifest holdout is required")
  const ids = new Set(), holdout = new Set(manifest.holdout), fixtureHashes = new Map(), oracleHashes = new Map()
  if (holdout.size !== manifest.holdout.length) fail("manifest holdout contains duplicate sample ids")
  for (const sample of manifest.corpus) {
    if (!sample.id || ids.has(sample.id) || !["benchmark", "holdout"].includes(sample.split)) fail(`invalid sample id/split: ${sample.id}`)
    ids.add(sample.id)
    if (sample.split === "holdout" && !holdout.has(sample.id)) fail(`holdout sample not listed: ${sample.id}`)
    if (sample.split === "benchmark" && holdout.has(sample.id)) fail(`benchmark sample listed as holdout: ${sample.id}`)
    if (!sample.fixture?.path || !/^[a-f0-9]{64}$/.test(sample.fixture.sha256)) fail(`invalid fixture binding: ${sample.id}`)
    if (!sample.oracle?.path || !/^[a-f0-9]{64}$/.test(sample.oracle.sha256)) fail(`invalid oracle binding: ${sample.id}`)
    const fixture = resolve(base, sample.fixture.path), oraclePath = resolve(base, sample.oracle.path)
    hashFile(fixture, sample.fixture.sha256, `fixture ${sample.id}`)
    hashFile(oraclePath, sample.oracle.sha256, `oracle ${sample.id}`)
    validateOracle(readJson(oraclePath), `oracle ${sample.id}`)
    for (const [label, hash, seen] of [["fixture", sample.fixture.sha256, fixtureHashes], ["oracle", sample.oracle.sha256, oracleHashes]]) {
      const previous = seen.get(hash)
      if (previous && previous.id !== sample.id && (sample.split !== previous.split || sample.split === "holdout")) fail(`${label} hash duplicated across benchmark/holdout: ${previous.id}, ${sample.id}`)
      seen.set(hash, { id: sample.id, split: sample.split })
    }
  }
  if ([...holdout].some(id => !ids.has(id) || manifest.corpus.find(sample => sample.id === id).split !== "holdout")) fail("holdout must reference only holdout samples")
  if (!manifest.corpus.some(sample => sample.split === "benchmark")) fail("manifest has no benchmark samples")
  return true
}

function parseResponse(raw) {
  try { const envelope = JSON.parse(raw), generated = typeof envelope?.message?.content === "string" ? envelope.message.content : raw, parsed = JSON.parse(generated); return parsed && typeof parsed === "object" ? parsed : null } catch { return null }
}
function score(raw, oracle, latency, timeoutSec, ui = {}) {
  const parsed = parseResponse(raw), text = parsed?.full_text
  const tokens = oracle.required_tokens
  const tokenCoverage = tokens.length ? tokens.filter(t => typeof text === "string" && text.includes(t)).length / tokens.length : 0
  const metadataKeys = Object.keys(oracle.metadata).sort()
  const metadataAccuracy = metadataKeys.length ? metadataKeys.filter(k => String(parsed?.key_metadata?.[k] ?? "") === String(oracle.metadata[k])).length / metadataKeys.length : 0
  const exactValues = metadataKeys.length ? metadataKeys.every(k => String(parsed?.key_metadata?.[k] ?? "") === String(oracle.metadata[k])) : false
  const spans = oracle.relevant_spans.length ? oracle.relevant_spans.every(s => Array.isArray(parsed?.relevant_spans) && parsed.relevant_spans.some(x => normalize(x.quote) === normalize(s.quote))) : false
  const sim = typeof text === "string" ? similarity(text, oracle.verbatim_body_text) : 0
  const speed = Math.max(0, Math.min(1, 1 - latency / (timeoutSec * 1000)))
  const uiPass = ui.synthetic_text === true && ui.no_image_file_parts === true && ui.local_transport === true && ui.remote_fallback === false
  const jsonValid = !!parsed && typeof parsed.content_type === "string" && typeof parsed.summary === "string" && typeof parsed.key_metadata === "object" && Array.isArray(parsed.relevant_spans) && typeof parsed.full_text === "string"
  const total = tokenCoverage * .3 + sim * .25 + metadataAccuracy * .2 + (exactValues ? .1 : 0) + (spans ? .1 : 0) + speed * .05
   const pass = jsonValid && exactValues && spans && uiPass && total >= TOTAL_SCORE_THRESHOLD
  return { json_valid: jsonValid, required_tokens_coverage: tokenCoverage, normalized_levenshtein_similarity: sim, structured_metadata_accuracy: metadataAccuracy, exact_value_safety: exactValues, span_safety: spans, speed_latency: speed, ui_assertions: { ...ui, pass: uiPass }, total, pass }
}

function scoreText(text, oracle, latency, timeoutSec) {
  const value = typeof text === "string" ? text : ""
  const tokenCoverage = oracle.required_tokens.length ? oracle.required_tokens.filter(t => value.includes(t)).length / oracle.required_tokens.length : 0
  const keys = Object.keys(oracle.metadata)
  const metadataAccuracy = keys.length ? keys.filter(k => value.includes(String(oracle.metadata[k]))).length / keys.length : 0
  const exactValues = keys.length > 0 && keys.every(k => value.includes(String(oracle.metadata[k])))
  const spans = oracle.relevant_spans.length > 0 && oracle.relevant_spans.every(s => normalize(value).includes(normalize(s.quote)))
  const sim = similarity(value, oracle.verbatim_body_text)
  const speed = Math.max(0, Math.min(1, 1 - latency / (timeoutSec * 1000)))
  return { required_tokens_coverage: tokenCoverage, normalized_levenshtein_similarity: sim, structured_metadata_accuracy: metadataAccuracy, exact_value_safety: exactValues, span_safety: spans, speed_latency: speed, total: tokenCoverage * .3 + sim * .25 + metadataAccuracy * .2 + (exactValues ? .1 : 0) + (spans ? .1 : 0) + speed * .05 }
}

function scoreExtract(raw, oracle, latency, timeoutSec, ui = {}) {
  let parsed = null
  try { parsed = JSON.parse(raw) } catch {}
  const authoritative = scoreText(parsed?.text, oracle, latency, timeoutSec)
  const corrected = scoreText(parsed?.corrected_text ?? parsed?.text, oracle, latency, timeoutSec)
  const uiPass = ui.synthetic_text === true && ui.no_image_file_parts === true && ui.local_transport === true && ui.remote_fallback === false
   return { json_valid: parsed?.status === "ok" && typeof parsed?.text === "string", authoritative, corrected, ui_assertions: { ...ui, pass: uiPass }, pass: parsed?.status === "ok" && authoritative.exact_value_safety && authoritative.span_safety && uiPass && authoritative.total >= TOTAL_SCORE_THRESHOLD }
}

function validateRecord(r) {
  if (!r || r.schema_version !== SCHEMA_VERSION || !r.run_id || !["apple", "tesseract", "production", "baseline", "candidate"].includes(r.role) || !r.model || !r.sample_id || r.split !== "benchmark" || typeof r.raw_output !== "string" || r.raw_output_sha256 !== sha256(r.raw_output) || !Array.isArray(r.errors) || !r.fixture?.path || !/^[a-f0-9]{64}$/.test(r.fixture.sha256) || !r.oracle?.path || !/^[a-f0-9]{64}$/.test(r.oracle.sha256)) fail("invalid record envelope")
  if (!Number.isFinite(r.latency_ms) || r.latency_ms < 0 || !r.rss || !Array.isArray(r.rss.samples) || !Number.isFinite(r.rss.peak_aggregate_mb) || r.rss.gate_pass !== (r.rss.peak_aggregate_mb * 1024 * 1024 < RSS_LIMIT_BYTES)) fail("invalid latency/RSS; RSS >= 40 GiB must fail")
   if (r.errors.some(e => /timeout/i.test(e)) || r.timed_out === true) fail("timed-out record rejected")
  if (!r.scores || typeof r.scores.pass !== "boolean" || (typeof r.scores.exact_value_safety !== "boolean" && typeof r.scores.authoritative?.exact_value_safety !== "boolean") || (typeof r.scores.span_safety !== "boolean" && typeof r.scores.authoritative?.span_safety !== "boolean")) fail("missing safety scores")
  return true
}
function validateFile(path) { const lines = readFileSync(path, "utf8").split("\n").filter(Boolean); if (!lines.length) fail("JSONL empty"); lines.forEach((x, i) => { try { validateRecord(JSON.parse(x)) } catch (e) { fail(`line ${i + 1}: ${e.message}`) } }); console.log(`VALID JSONL: ${path} (${lines.length} records)`) }
function validateArtifact(path, manifest, base) {
  validateFile(path)
  const byId = new Map(manifest.corpus.map(sample => [sample.id, sample]))
  const records = readFileSync(path, "utf8").split("\n").filter(Boolean).map(JSON.parse)
  const runIds = new Set()
  for (const record of records) {
    if (runIds.has(record.run_id)) fail(`duplicate run_id: ${record.run_id}`)
    runIds.add(record.run_id)
    const sample = byId.get(record.sample_id)
    if (!sample || sample.split !== record.split || sample.fixture.path !== record.fixture?.path || sample.fixture.sha256 !== record.fixture?.sha256 || sample.oracle.path !== record.oracle?.path || sample.oracle.sha256 !== record.oracle?.sha256) fail(`record ${record.run_id} is not bound to manifest sample ${record.sample_id}`)
    hashFile(resolve(base, record.fixture.path), record.fixture.sha256, `fixture ${record.sample_id}`)
    hashFile(resolve(base, record.oracle.path), record.oracle.sha256, `oracle ${record.sample_id}`)
    hashFile(join(resolve(path, ".."), `raw-${record.run_id}.txt`), record.raw_output_sha256, `raw output ${record.run_id}`)
    if (record.split === "holdout" || manifest.holdout.includes(record.sample_id)) fail(`holdout sample was executed: ${record.sample_id}`)
  }
  const requiredRoles = records.some(record => ["apple", "tesseract", "production"].includes(record.role)) ? ["apple", "tesseract", "production"] : ["baseline", "candidate"]
  for (const sample of manifest.corpus.filter(x => x.split === "benchmark")) for (const role of requiredRoles) if (!records.some(record => record.sample_id === sample.id && record.role === role)) fail(`missing ${role} records for benchmark sample: ${sample.id}`)
  if (requiredRoles.includes("production")) {
    for (const sample of manifest.corpus.filter(x => x.split === "benchmark")) {
      const production = records.find(record => record.sample_id === sample.id && record.role === "production")
      let productionPayload = null
      try { productionPayload = JSON.parse(production.raw_output) } catch {}
      const direct = records.filter(record => record.sample_id === sample.id && ["apple", "tesseract"].includes(record.role))
      const directScores = direct.map(record => {
        let payload = null
        try { payload = JSON.parse(record.raw_output) } catch {}
        return { role: record.role, quality: ocrQuality(payload?.text, payload?.meta?.confidence ?? payload?.meta?.apple_vision?.confidence) }
      })
      const expected = directScores.reduce((best, current) => current.quality > best.quality ? current : best, { role: "", quality: -1 })
      if (!productionPayload?.tool || productionPayload.tool !== expected.role) fail(`production selection mismatch for ${sample.id}: selected=${productionPayload?.tool ?? "none"} expected=${expected.role}`)
    }
  }
  return records
}
function metrics(records) {
  const scores = records.map(r => r.scores)
  const mean = key => scores.reduce((sum, score) => sum + Number(score[key] ?? 0), 0) / Math.max(scores.length, 1)
  return { records: records.length, pass_rate: scores.filter(s => s.pass).length / Math.max(scores.length, 1), mean_total: mean("total"), mean_latency_ms: records.reduce((sum, r) => sum + r.latency_ms, 0) / Math.max(records.length, 1), exact_value_safety_rate: scores.filter(s => s.exact_value_safety).length / Math.max(scores.length, 1), span_safety_rate: scores.filter(s => s.span_safety).length / Math.max(scores.length, 1), ui_assertion_pass_rate: scores.filter(s => s.ui_assertions?.pass).length / Math.max(scores.length, 1) }
}

function extractMetrics(records) {
  const mean = branch => records.reduce((sum, r) => sum + Number(r.scores[branch]?.total ?? 0), 0) / Math.max(records.length, 1)
  return {
    records: records.length,
    pass_rate: records.filter(r => r.scores.pass).length / Math.max(records.length, 1),
    mean_authoritative_total: mean("authoritative"),
    mean_corrected_total: mean("corrected"),
    mean_latency_ms: records.reduce((sum, r) => sum + r.latency_ms, 0) / Math.max(records.length, 1),
    authoritative_exact_value_safety_rate: records.filter(r => r.scores.authoritative?.exact_value_safety).length / Math.max(records.length, 1),
    corrected_exact_value_safety_rate: records.filter(r => r.scores.corrected?.exact_value_safety).length / Math.max(records.length, 1),
    authoritative_span_safety_rate: records.filter(r => r.scores.authoritative?.span_safety).length / Math.max(records.length, 1),
    corrected_span_safety_rate: records.filter(r => r.scores.corrected?.span_safety).length / Math.max(records.length, 1),
    ui_assertion_pass_rate: records.filter(r => r.scores.ui_assertions?.pass).length / Math.max(records.length, 1),
  }
}

function engineMetrics(records) {
  return Object.fromEntries([...new Set(records.map(r => r.role))].map(role => [role, extractMetrics(records.filter(r => r.role === role))]))
}

function compareCandidates(baseline, candidate) {
  const baselineTotal = baseline.mean_total ?? baseline.mean_authoritative_total ?? 0
  const candidateTotal = candidate.mean_total ?? candidate.mean_authoritative_total ?? 0
  const threshold = baselineTotal * NON_REGRESSION_THRESHOLD
  return {
    threshold,
    candidate_score: candidateTotal,
    baseline_score: baselineTotal,
    pass: candidateTotal >= threshold,
  }
}

function reproducibility(records) {
  const groups = new Map()
  for (const record of records) {
    const key = `${record.sample_id}\0${record.role}`
    const group = groups.get(key) ?? []
    group.push(record)
    groups.set(key, group)
  }
  const failures = []
  for (const [key, group] of groups) {
    const hashes = new Set(group.map(record => record.raw_output_sha256))
    if (group.length < 2 || hashes.size !== 1 || group.some(record => record.errors.length || record.timed_out)) failures.push(key)
  }
  return { pass: failures.length === 0, groups: groups.size, failures }
}

function validateHoldoutEvaluation(path, manifest, manifestHash, candidateModel, baselineModel) {
  if (!path) fail("frozen holdout evaluation artifact is required")
  const bytes = readFileSync(resolve(path))
  const evaluation = readJson(path)
  const base = resolve(path, "..")
  if (evaluation.schema_version !== 2 || evaluation.frozen !== true || evaluation.split !== "holdout" || evaluation.review !== "independent" || evaluation.manifest_sha256 !== manifestHash || evaluation.candidate_model !== candidateModel || evaluation.decision !== "independent-held-out-review" || !Array.isArray(evaluation.holdout_ids)) fail("invalid frozen holdout evaluation artifact")
  if (!evaluation.results?.path || !/^[a-f0-9]{64}$/.test(evaluation.results.sha256) || !evaluation.scorer?.path || !/^[a-f0-9]{64}$/.test(evaluation.scorer.sha256)) fail("holdout results and scorer must be hash-bound")
  const holdoutIds = manifest.corpus.filter(sample => sample.split === "holdout").map(sample => sample.id).sort()
  if (JSON.stringify([...evaluation.holdout_ids].sort()) !== JSON.stringify(holdoutIds)) fail("holdout evaluation IDs do not match manifest")
  const resultsPath = resolve(base, evaluation.results.path)
  const scorerPath = resolve(base, evaluation.scorer.path)
  hashFile(resultsPath, evaluation.results.sha256, "holdout results")
  hashFile(scorerPath, evaluation.scorer.sha256, "scorer")
  const scorer = readJson(scorerPath)
  if (scorer.schema_version !== 1 || scorer.thresholds?.total !== TOTAL_SCORE_THRESHOLD || scorer.thresholds?.non_regression !== NON_REGRESSION_THRESHOLD) fail("scorer thresholds are not frozen")
  const samples = new Map(manifest.corpus.filter(sample => sample.split === "holdout").map(sample => [sample.id, sample]))
  const records = readFileSync(resultsPath, "utf8").split("\n").filter(Boolean).map(JSON.parse)
  for (const record of records) {
    const sample = samples.get(record.sample_id)
    if (!sample || record.schema_version !== SCHEMA_VERSION || record.split !== "holdout" || !["baseline", "candidate"].includes(record.role) || record.model !== (record.role === "candidate" ? candidateModel : baselineModel) || sample.fixture.path !== record.fixture?.path || sample.fixture.sha256 !== record.fixture?.sha256 || sample.oracle.path !== record.oracle?.path || sample.oracle.sha256 !== record.oracle?.sha256 || typeof record.raw_output !== "string" || record.raw_output_sha256 !== sha256(record.raw_output) || !Array.isArray(record.errors) || record.errors.length || record.timed_out || record.rss?.gate_pass !== true || record.rss.peak_aggregate_mb * 1024 * 1024 >= RSS_LIMIT_BYTES || record.scores?.pass !== true || Number(record.scores?.total) < TOTAL_SCORE_THRESHOLD || record.scores?.exact_value_safety !== true || record.scores?.span_safety !== true || record.scores?.ui_assertions?.pass !== true) fail(`invalid holdout record: ${record.run_id ?? "unknown"}`)
  }
  for (const sampleId of samples.keys()) for (const role of ["baseline", "candidate"]) {
    const group = records.filter(record => record.sample_id === sampleId && record.role === role)
    if (group.length < 2 || new Set(group.map(record => record.raw_output_sha256)).size !== 1) fail(`holdout reproducibility failed: ${sampleId}/${role}`)
  }
  const mean = role => records.filter(record => record.role === role).reduce((sum, record) => sum + Number(record.scores.total), 0) / Math.max(records.filter(record => record.role === role).length, 1)
  if (mean("candidate") < mean("baseline") * NON_REGRESSION_THRESHOLD) fail("holdout candidate regressed below baseline")
  return { path: resolve(path), sha256: sha256(bytes) }
}

async function installedVisionModels() { const r = await fetch(`${OLLAMA_URL}/api/tags`); if (!r.ok) fail(`Ollama tags HTTP ${r.status}`); return ((await r.json()).models ?? []).map(m => m.name).sort() }
function sampleRss(pids, interval, sink) { let timer, pending = Promise.resolve(); const sample = () => { const p = spawn("ps", ["-o", "pid=,rss=", "-p", [process.pid, ...pids].filter(Boolean).join(",")]); let text = ""; p.stdout.on("data", d => { text += d }); pending = new Promise(done => p.on("close", () => { const values = text.trim().split("\n").map(x => x.trim().split(/\s+/).map(Number)).filter(x => x.length === 2 && x.every(Number.isFinite)); const client = values.find(([pid]) => pid === process.pid)?.[1] ?? 0, ollama = values.filter(([pid]) => pids.includes(pid)).reduce((n, [, rss]) => n + rss, 0); sink.push({ at: new Date().toISOString(), aggregate_mb: (client + ollama) / 1024, client_mb: client / 1024, ollama_mb: ollama / 1024 }); done() })) }; sample(); timer = setInterval(sample, interval); return async () => { clearInterval(timer); await pending; return sink } }
async function pids() { return new Promise(resolvePids => { const p = spawn("pgrep", ["-f", "ollama"]); let text = ""; p.stdout.on("data", d => { text += d }); p.on("close", () => resolvePids(text.trim().split("\n").map(Number).filter(Boolean))) }) }
async function infer(model, image, prompt, timeoutSec, sampleMs) { const samples = [], stop = sampleRss(await pids(), sampleMs, samples), started = Date.now(), controller = new AbortController(), errors = []; let raw = ""; const timeout = setTimeout(() => controller.abort(), timeoutSec * 1000); try { const r = await fetch(`${OLLAMA_URL}/api/chat`, { method: "POST", signal: controller.signal, headers: { "content-type": "application/json" }, body: JSON.stringify({ model, messages: [{ role: "user", content: prompt, images: [image] }], stream: false, think: false, format: "json", options: { temperature: 0, num_ctx: 16384 } }) }); raw = await r.text(); if (!r.ok) errors.push(`Ollama HTTP ${r.status}`) } catch (e) { errors.push(e.name === "AbortError" ? `timeout after ${timeoutSec}s` : String(e.message)) } finally { clearTimeout(timeout) } const rss = await stop(), peak = Math.max(0, ...rss.map(x => x.aggregate_mb)); return { raw, errors, latencyMs: Date.now() - started, rss, peak } }
async function inferExtract(script, fixture, mode, timeoutSec, sampleMs, warm, cacheDir) {
  const samples = [], started = Date.now(), errors = []
  const childArgs = [script, fixture, "--ocr-engine", mode, "--timeout", String(timeoutSec)]
  if (!warm) childArgs.push("--cache-bypass-read")
  const child = spawn("python3", childArgs, { env: { ...process.env, OCR_PRODUCTION_ENABLED: "0", MEDIA_CACHE_DIR: cacheDir }, stdio: ["ignore", "pipe", "pipe"] })
  const stop = sampleRss(child.pid ? [child.pid] : [], sampleMs, samples)
  let raw = "", stderr = ""
  child.stdout.on("data", data => { raw += data })
  child.stderr.on("data", data => { stderr += data })
  const timer = setTimeout(() => child.kill("SIGTERM"), timeoutSec * 1000)
  await new Promise(resolveP => child.on("close", code => { clearTimeout(timer); if (code !== 0) errors.push(`extract.py exit ${code}`); if (stderr.trim()) errors.push(stderr.trim().slice(-400)); resolveP() }))
  const rss = await stop(), peak = Math.max(0, ...rss.map(x => x.aggregate_mb))
  return { raw: raw.trim(), errors, latencyMs: Date.now() - started, rss, peak }
}
function writeImmutable(path, content) { if (existsSync(path)) fail(`immutable artifact already exists: ${path}`); writeFileSync(path, content, { flag: "wx" }) }
function selfTestSchemaGates() {
  const base = { schema_version: SCHEMA_VERSION, run_id: "x", role: "candidate", model: "local", sample_id: "x", split: "benchmark", fixture: { path: "x", sha256: "0".repeat(64) }, oracle: { path: "x", sha256: "0".repeat(64) }, raw_output: "{}", raw_output_sha256: sha256("{}"), errors: [], latency_ms: 1, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: { pass: false, exact_value_safety: false, span_safety: false } }
  for (const [name, record] of [["holdout split", { ...base, split: "holdout" }], ["missing model", { ...base, model: undefined }], ["raw hash", { ...base, raw_output_sha256: "0".repeat(64) }]]) {
    try { validateRecord(record); fail(`${name} accepted`) } catch (e) { if (!e.message.startsWith("INVALID:")) throw e }
  }
  const path = join(tmpdir(), `benchmark-immutable-${randomUUID()}`)
  writeImmutable(path, "one")
  try { writeImmutable(path, "two"); fail("immutable overwrite accepted") } catch (e) { if (!e.message.startsWith("INVALID:")) throw e }
}
function selfTest() { const oracle = { schema_version: 1, authorship: "human", provenance: "blinded-human", verbatim_body_text: "ID A-1", required_tokens: ["A-1"], metadata: { id: "A-1" }, relevant_spans: [{ quote: "A-1" }] }; const ui = { synthetic_text: true, no_image_file_parts: true, local_transport: true, remote_fallback: false }; const goodRaw = JSON.stringify({ message: { content: JSON.stringify({ content_type: "text", summary: "x", key_metadata: { id: "A-1" }, relevant_spans: [{ quote: "A-1" }], full_text: "ID A-1" }) } }); const good = score(goodRaw, oracle, 1, 120, ui); if (!good.pass) fail("synthetic valid case did not pass"); for (const badScore of [score(goodRaw.replace("A-1", "A-2"), oracle, 1, 120, ui), score(goodRaw, oracle, 1, 120, { ...ui, remote_fallback: true })]) if (badScore.pass) fail("unsafe output accepted"); for (const bad of [{ ...oracle, authorship: "model" }, { ...oracle, provenance: undefined }]) { try { validateOracle(bad); fail("rejection case accepted") } catch (e) { if (!e.message.startsWith("INVALID:")) throw e } } for (const bad of [{ schema_version: 2, run_id: "x", role: "candidate", sample_id: "x", split: "benchmark", fixture: { path: "x", sha256: "0".repeat(64) }, oracle: { path: "x", sha256: "0".repeat(64) }, raw_output: "", errors: ["timeout"], latency_ms: 1, timed_out: true, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: good }, { schema_version: 2, run_id: "x", role: "candidate", sample_id: "x", split: "benchmark", fixture: { path: "x", sha256: "0".repeat(64) }, oracle: { path: "x", sha256: "0".repeat(64) }, raw_output: "", errors: [], latency_ms: 1, rss: { samples: [], peak_aggregate_mb: 40960, gate_pass: false }, scores: good }, { schema_version: 2, run_id: "x", role: "other", sample_id: "x", split: "benchmark", fixture: { path: "x", sha256: "0".repeat(64) }, oracle: { path: "x", sha256: "0".repeat(64) }, raw_output: "", errors: [], latency_ms: 1, rss: { samples: [], peak_aggregate_mb: 1, gate_pass: true }, scores: good }]) { try { validateRecord(bad); fail("invalid record accepted") } catch (e) { if (!e.message.startsWith("INVALID:")) throw e } } console.log("SELF-TEST PASS: valid, unsafe-value, unsafe-UI, model-authored, missing-provenance, timeout, RSS, and invalid-sample cases") }

async function main() {
  const o = args(process.argv.slice(2)); if (o.help) return help(); if (o.selfTest) { selfTest(); selfTestSchemaGates(); return }
  if (!o.manifest) fail("--manifest required; unbound fixture/oracle inputs rejected")
  const manifestPath = resolve(o.manifest), manifestBase = resolve(manifestPath, ".."), manifestBytes = readFileSync(manifestPath), manifestHash = sha256(manifestBytes)
  const manifest = readJson(manifestPath); validateManifest(manifest, manifestBase)
  const holdoutEvaluation = o.dryRun || o.check ? null : validateHoldoutEvaluation(o.holdoutEvaluation, manifest, manifestHash, o.extractPipeline ? "extract.py:production" : o.model, o.extractPipeline ? "extract.py:tesseract" : o.baselineModel)
  const ui = readJson(resolve(manifestBase, manifest.ui_assertions.path))
  if (o.validate) { validateArtifact(o.validate, manifest, manifestBase); console.log(`VALID AUDIT ARTIFACT: ${o.validate}`); return }
   if (o.dryRun && !o.check) { console.log(JSON.stringify({ dry_run: true, schema_version: SCHEMA_VERSION, pipeline: o.extractPipeline ? "extract.py" : "ollama", candidate_model: o.extractPipeline ? "extract.py:correction" : o.model, baseline_model: o.extractPipeline ? "extract.py:baseline" : o.baselineModel, extract: o.extract, samples: manifest.corpus.map(x => x.id), holdout: manifest.holdout ?? [], timeout_sec: o.timeoutSec, rss_limit_bytes: RSS_LIMIT_BYTES, total_score_threshold: TOTAL_SCORE_THRESHOLD, non_regression_threshold: NON_REGRESSION_THRESHOLD, gates: ["detection", "text", "privacy", "rss", "latency", "reproducibility", "non_regression"], timeout_is_failure: true, remote_fallback: false, production_enabled: false, immutable_artifacts: true }, null, 2)); return }
   if (o.extractPipeline) {
    if (!existsSync(o.extract)) fail(`extract.py missing: ${o.extract}`)
    if (existsSync(o.out)) fail(`output directory already exists: ${o.out}`)
    const stamp = new Date().toISOString().replace(/[:.]/g, "-") + "-" + randomUUID()
    mkdirSync(o.out)
    const jsonl = join(o.out, `results-${stamp}.jsonl`), records = []
     for (const sample of manifest.corpus.filter(x => x.split === "benchmark" && !manifest.holdout.includes(x.id))) {
       const fixture = resolve(manifestBase, sample.fixture.path), oracle = readJson(resolve(manifestBase, sample.oracle.path))
        for (const [role, engine] of [["apple", "apple"], ["tesseract", "tesseract"], ["production", "production"]]) {
        const cacheDir = join(o.out, `cache-${role}`)
        mkdirSync(cacheDir)
        for (let i = 0; i <= o.repetitions; i++) {
         const result = await inferExtract(o.extract, fixture, engine, o.timeoutSec, o.sampleMs, i > 0, cacheDir)
        let parsed = null
        try { parsed = JSON.parse(result.raw) } catch {}
        if (parsed?.provenance?.production_enabled) fail("production pipeline enabled")
         const record = { schema_version: SCHEMA_VERSION, run_id: `${stamp}-${sample.id}-${role}-${i}`, role, model: `extract.py:${engine}`, engine, sample_id: sample.id, split: sample.split, fixture: sample.fixture, oracle: sample.oracle, state: i === 0 ? "cold" : (parsed?.cached === true ? `warm-cached-${i}` : `warm-miss-${i}`), cache_hit: parsed?.cached === true, latency_ms: result.latencyMs, timed_out: result.errors.some(e => /timeout/i.test(e)), rss: { samples: result.rss, peak_aggregate_mb: result.peak, gate_pass: result.peak * 1024 * 1024 < RSS_LIMIT_BYTES }, scores: scoreExtract(result.raw, oracle, result.latencyMs, o.timeoutSec, ui), provenance: parsed?.provenance ?? { production_enabled: false }, raw_output: result.raw, raw_output_sha256: sha256(result.raw), errors: result.errors }
        validateRecord(record)
        if (!record.rss.gate_pass) fail("40 GiB RSS gate exceeded")
        writeImmutable(join(o.out, `raw-${record.run_id}.txt`), result.raw)
        records.push(record)
        }
      }
    }
    writeImmutable(jsonl, records.map(record => JSON.stringify(record)).join("\n") + "\n")
    validateFile(jsonl)
       const resultsHash = sha256(readFileSync(jsonl)), byEngine = engineMetrics(records), baselineRecords = records.filter(r => r.role === "tesseract"), candidateRecords = records.filter(r => r.role === "production"), reproducibilityGate = reproducibility(records)
      const comparison = compareCandidates(extractMetrics(baselineRecords), extractMetrics(candidateRecords))
       const decision = { schema_version: SCHEMA_VERSION, manifest: { path: o.manifest, sha256: manifestHash }, results: { path: jsonl, sha256: resultsHash }, holdout_evaluation: holdoutEvaluation, pipeline: "extract.py", candidate_model: "extract.py:production", baseline_model: "extract.py:tesseract", metrics: byEngine, comparison: { ...comparison, compared: ["tesseract", "production"], apple: byEngine.apple, authoritative_field: "text/original_text", production_enabled: false }, holdout: manifest.holdout ?? [], ui_assertions: manifest.ui_assertions, gates: { schema: true, manifest_identity: true, provenance: true, holdout: true, holdout_evaluation: true, remote_fallback: true, production_disabled: true, rss: records.every(r => r.rss.gate_pass), latency: records.every(r => !r.timed_out && !r.errors.length), reproducibility: reproducibilityGate.pass, safety: records.every(r => r.scores.authoritative.exact_value_safety && r.scores.authoritative.span_safety), non_regression: comparison.pass }, decision: "computed-from-validated-records", remote_fallback: false }
       if (!comparison.pass) fail(`production extractor regressed below ${NON_REGRESSION_THRESHOLD * 100}% of Tesseract`)
     if (records.some(r => r.timed_out || r.errors.length)) fail("latency/error gate failed")
     if (!reproducibilityGate.pass) fail(`reproducibility gate failed: ${reproducibilityGate.failures.join(", ")}`)
    writeImmutable(join(o.out, `decision-${stamp}.json`), JSON.stringify(decision, null, 2))
    console.log(JSON.stringify({ jsonl, artifacts: o.out, pipeline: "extract.py", production_enabled: false }))
    return
  }
  if (!o.extractPipeline) {
    const models = await installedVisionModels(); if (!models.includes(o.model) || !models.includes(o.baselineModel)) fail("candidate and baseline must be installed local vision models")
    if (o.check) { console.log(JSON.stringify({ check: true, manifest: o.manifest, manifest_sha256: manifestHash, candidate_model: o.model, baseline_model: o.baselineModel, installed_vision_models: models }, null, 2)); return }
  }
  if (existsSync(o.out)) fail(`output directory already exists: ${o.out}`)
  const stamp = new Date().toISOString().replace(/[:.]/g, "-") + "-" + randomUUID(); mkdirSync(o.out); const jsonl = join(o.out, `results-${stamp}.jsonl`), prompt = "Extract attached media as JSON. Preserve exact values and verbatim spans. Never guess.", records = []
  for (const sample of manifest.corpus.filter(x => x.split === "benchmark" && !manifest.holdout.includes(x.id))) for (const [role, model] of [["baseline", o.baselineModel], ["candidate", o.model]]) for (let i = 0; i <= o.repetitions; i++) { const image = readFileSync(resolve(manifestBase, sample.fixture.path)).toString("base64"), oracle = readJson(resolve(manifestBase, sample.oracle.path)), result = await infer(model, image, prompt, o.timeoutSec, o.sampleMs), record = { schema_version: SCHEMA_VERSION, run_id: `${stamp}-${sample.id}-${role}-${i}`, role, model, sample_id: sample.id, split: sample.split, fixture: sample.fixture, oracle: sample.oracle, state: i === 0 ? "cold" : `warm-${i}`, latency_ms: result.latencyMs, timed_out: result.errors.some(e => /timeout/i.test(e)), rss: { samples: result.rss, peak_aggregate_mb: result.peak, gate_pass: result.peak * 1024 * 1024 < RSS_LIMIT_BYTES }, scores: score(result.raw, oracle, result.latencyMs, o.timeoutSec, ui), raw_output: result.raw, raw_output_sha256: sha256(result.raw), errors: result.errors }; validateRecord(record); if (!record.rss.gate_pass) fail("40 GiB RSS gate exceeded"); writeImmutable(join(o.out, `raw-${record.run_id}.txt`), result.raw); records.push(record) }
  writeImmutable(jsonl, records.map(record => JSON.stringify(record)).join("\n") + "\n")
  validateFile(jsonl)
   const resultsHash = sha256(readFileSync(jsonl))
  const baselineRecords = records.filter(r => r.role === "baseline")
  const candidateRecords = records.filter(r => r.role === "candidate")
   const comparison = compareCandidates(metrics(baselineRecords), metrics(candidateRecords))
   const reproducibilityGate = reproducibility(records)
   if (!comparison.pass) fail(`candidate regressed below ${NON_REGRESSION_THRESHOLD * 100}% of baseline`)
     if (records.some(r => r.timed_out || r.errors.length)) fail("latency/error gate failed")
   if (!reproducibilityGate.pass) fail(`reproducibility gate failed: ${reproducibilityGate.failures.join(", ")}`)
  const decision = {
    schema_version: SCHEMA_VERSION,
    manifest: { path: o.manifest, sha256: manifestHash },
     results: { path: jsonl, sha256: resultsHash },
     holdout_evaluation: holdoutEvaluation,
    candidate_model: o.model,
    baseline_model: o.baselineModel,
    metrics: { baseline: metrics(baselineRecords), candidate: metrics(candidateRecords) },
    comparison,
    holdout: manifest.holdout ?? [],
    ui_assertions: manifest.ui_assertions,
    gates: {
      schema: true,
      manifest_identity: true,
      provenance: true,
       holdout: true,
       holdout_evaluation: true,
      remote_fallback: true,
       rss: records.every(r => r.rss.gate_pass),
       safety: records.every(r => r.scores.exact_value_safety && r.scores.span_safety),
        latency: records.every(r => !r.timed_out && !r.errors.length),
        reproducibility: reproducibilityGate.pass,
        non_regression: comparison.pass,
    },
    decision: "computed-from-validated-records",
    remote_fallback: false,
  }
  writeImmutable(join(o.out, `decision-${stamp}.json`), JSON.stringify(decision, null, 2))
  console.log(JSON.stringify({ jsonl, artifacts: o.out }))
}
main().catch(e => { console.error(`ERROR: ${e.message}`); process.exitCode = 1 })
