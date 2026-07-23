// media-guard — keep unsupported media attachments (PDF / audio / video / image)
// from crashing a session, and inline locally-extracted text instead.
//
// Problem: when the user attaches/references a PDF (or audio, video, image),
// opencode builds a file part with that mime and ships the raw bytes to the
// provider. Providers that reject the media type fail the ENTIRE request with
// e.g. 'file part media type application/pdf' functionality not supported, which
// makes the session unusable.
//
// Fix: in experimental.chat.messages.transform (the last stop before the LLM
// call) swap every file part whose mime is in the unsupported set for a
// synthetic TEXT part. A deterministic local dispatcher (media/extract.py)
// produces the text: pdftotext/PyMuPDF/pypdf for PDF, whisper for audio/video,
// tesseract for images. Results are cached (in-memory here + on disk in
// extract.py). If extraction is unavailable we emit a pointer note routing the
// agent to the matching skill (tool--pdf / tool--transcribe). No media bytes
// ever reach the remote, so the session keeps working.
//
// This generalizes the former pdf-guard.ts and consolidates the extraction
// logic that used to live in the tool--pdf and tool--transcribe skills.
//
// Also registers a `media_extract` tool so the agent can parse a media file
// on demand (e.g. a larger model, or the full untruncated text).

import { tool, type Plugin } from "@opencode-ai/plugin"
import { tmpdir } from "node:os"
import { join, dirname, resolve } from "node:path"
import { writeFileSync, statSync, readFileSync, mkdirSync, existsSync } from "node:fs"
import { createHash, createPublicKey, verify as verifySignature } from "node:crypto"

type Options = {
  // mime patterns to intercept. "*" wildcard suffix supported (e.g. "audio/*").
  mimes?: string[]
  // also intercept images (OCR). Off by default — many sessions want to *see*
  // images via vision models rather than OCR them.
  ocrImages?: boolean
  // Max characters of extracted text to inline per file.
  maxChars?: number
  // Whisper model for audio/video transcription.
  model?: string
  // Per-file extraction timeout (seconds) on the send-path.
  timeoutSec?: number
  // Media kinds routed to the cheap agent. Default ["image"].
  agentKinds?: string[]
  // Bounded parallelism for the transform fan-out. Default 6.
  concurrency?: number
  // If a single transform has MORE than this many matched media parts,
  // force deterministic local extraction (extract.py OCR) instead of local vision. Default 24.
  batchThreshold?: number
  // Global wall-clock budget for the whole transform. Default 240.
  transformBudgetSec?: number
  // Route image/video digests through a LOCAL ollama vision model first.
  visionEnabled?: boolean
  // Ollama vision model tag.
  visionModel?: string
  // Ollama base URL.
  visionBaseUrl?: string
  // Per-request timeout for local vision (seconds). Default 120.
  visionTimeoutSec?: number
  // Ollama context window (num_ctx) for local vision. Default 16384.
  // Caps KV-cache memory; the model's max (262k) would balloon RAM to ~26GB.
  visionNumCtx?: number
  visionCandidateEnabled?: boolean
  visionCandidateModel?: string
  evidencePath?: string
  evidenceSha256?: string
  holdoutEvaluationPath?: string
  holdoutEvaluationSha256?: string
  trustedAttestationKeys?: Record<string, string>
  revokedAttestationKeys?: string[]
  configSha256?: string
  // Optional local OCR correction. Disabled by default and evidence-gated.
  ocrCorrection?: boolean
  // Enable extractor's bounded disk cache. Enabled by default for deterministic results.
  extractorCache?: boolean
}

type Admission = { evidence: boolean; stickyNotes: boolean }

const FROZEN_SCORER = {
  schema_version: 1,
  scorer_version: "media-guard-score-v1",
  thresholds: { total: 0.8, non_regression: 0.95 },
  semantics: "oracle.notes text tokens are case-folded Unicode words; every token must occur in serialized output; every output note must contain box or bbox array; total is mean(exact_value_safety, span_safety)",
} as const

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`
  return `{${Object.keys(value as Record<string, unknown>).sort().map(key => `${JSON.stringify(key)}:${canonical((value as Record<string, unknown>)[key])}`).join(",")}}`
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex")
}

function publicKeyBytes(value: string): Buffer {
  if (value.includes("BEGIN PUBLIC KEY")) return createPublicKey(value).export({ type: "spki", format: "der" }) as Buffer
  return Buffer.from(value, "base64")
}

function scoreFromBoundRecord(record: any, oracleBytes: Buffer): { total: number; exact_value_safety: boolean; span_safety: boolean } | null {
  if (!record || typeof record.raw_output !== "string" || !record.raw_output_sha256 || sha256(record.raw_output) !== record.raw_output_sha256) return null
  if (!record.oracle?.sha256 || sha256(oracleBytes) !== String(record.oracle.sha256).toLowerCase()) return null
  try {
    const output = JSON.parse(record.raw_output)
    const oracle = JSON.parse(oracleBytes.toString("utf8"))
    const expected = Array.isArray(oracle.notes) ? oracle.notes : Array.isArray(oracle) ? oracle : null
    const actual = Array.isArray(output.notes) ? output.notes : Array.isArray(output.detections) ? output.detections : null
    if (!expected || !actual) return null
    const expectedTokens = expected.flatMap((note: any) => String(note?.text ?? note?.content ?? "").toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [])
    const actualText = JSON.stringify(actual).toLowerCase()
    const exact = expectedTokens.length > 0 && expectedTokens.every((token: string) => actualText.includes(token))
    const span = actual.every((note: any) => Array.isArray(note?.box ?? note?.bbox))
    const total = (Number(exact) + Number(span)) / 2
    return { total, exact_value_safety: exact, span_safety: span }
  } catch {
    return null
  }
}

function recomputedScore(record: any, base: string): { total: number; exact_value_safety: boolean; span_safety: boolean } | null {
  try {
    const oraclePath = resolve(base, String(record.oracle.path))
    const oracleBytes = readFileSync(oraclePath)
    if (sha256(oracleBytes) !== String(record.oracle.sha256).toLowerCase()) return null
    return scoreFromBoundRecord(record, oracleBytes)
  } catch {
    return null
  }
}

function verifyAttestation(artifact: any, evidenceHash: string, candidate: string, baseline: string, configHash: string, scorerHash: string, opts: Options): boolean {
  const reject = (reason: string) => {
    if (process.env.MEDIA_GUARD_DEBUG) console.error("[media-guard] attestation reject:", reason)
    return false
  }
  const attestation = artifact?.attestation
  if (!attestation || attestation.schema_version !== 1 || attestation.algorithm !== "Ed25519" || typeof attestation.key_id !== "string" || typeof attestation.signature !== "string") return reject("shape")
  const trusted = opts.trustedAttestationKeys?.[attestation.key_id]
   if (!trusted || (opts.revokedAttestationKeys ?? []).includes(attestation.key_id)) return reject("trust")
  const now = Date.now()
  const issued = Date.parse(String(attestation.issued_at))
  const expires = Date.parse(String(attestation.expires_at))
   if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now || expires <= now) return reject("time")
   if (attestation.candidate_model !== candidate || attestation.baseline_model !== baseline) return reject("models")
  const unsignedArtifact = { ...artifact }
  delete unsignedArtifact.attestation
   if (attestation.evidence_sha256 !== sha256(canonical(unsignedArtifact))) return reject("evidence binding")
   if (!/^[a-f0-9]{64}$/i.test(evidenceHash)) return reject("evidence hash")
   if (!opts.configSha256 || configHash !== opts.configSha256 || attestation.config_sha256 !== opts.configSha256) return reject("config")
   if (attestation.scorer_sha256 !== scorerHash) return reject(`scorer ${attestation.scorer_sha256} != ${scorerHash}`)
   if (!Array.isArray(attestation.capabilities) || attestation.capabilities.some((cap: unknown) => typeof cap !== "string")) return reject("capabilities")
   if ("previous_hash" in attestation || "sequence" in attestation) return reject("chain fields")
  const signed = { ...attestation }
  delete signed.signature
  try {
    const key = publicKeyBytes(trusted)
    const signature = Buffer.from(attestation.signature, "base64")
     return signature.length === 64 && verifySignature(null, Buffer.from(canonical(signed)), { key, format: "der", type: "spki" }, signature) || reject("signature")
  } catch {
    return false
  }
}

const DEFAULT_MIMES = ["application/pdf", "audio/*", "video/*"]
const ZIP_MIMES = ["application/zip", "application/x-zip", "application/x-zip-compressed", "application/zip-compressed", "multipart/x-zip"]
const MAX_ZIP_DEPTH = 2
const EXTRACT = new URL("./media/extract.py", import.meta.url).pathname
const DATAURL_DIR = join(tmpdir(), "opencode-media-guard-data")

function verifiedEvidence(path?: string, expectedHash?: string, expectedCandidate?: string, expectedBaseline?: string, opts: Options = {}): Admission {
  const rejected: Admission = { evidence: false, stickyNotes: false }
  if (!path || !expectedHash || !/^[a-f0-9]{64}$/i.test(expectedHash)) return rejected
  try {
    const bytes = readFileSync(path)
    const actual = createHash("sha256").update(bytes).digest("hex")
    if (actual.toLowerCase() !== expectedHash.toLowerCase()) return rejected
    const artifact = JSON.parse(bytes.toString("utf8"))
    const gates = artifact?.gates
    const metrics = artifact?.metrics
    if (!(artifact?.schema_version === 2 &&
      artifact?.decision === "computed-from-validated-records" &&
      typeof artifact?.manifest?.path === "string" &&
      /^[a-f0-9]{64}$/i.test(artifact?.manifest?.sha256 ?? "") &&
      typeof artifact?.candidate_model === "string" &&
      typeof artifact?.baseline_model === "string" &&
      artifact.candidate_model !== artifact.baseline_model &&
      (!expectedCandidate || artifact.candidate_model === expectedCandidate) &&
      (!expectedBaseline || artifact.baseline_model === expectedBaseline) &&
      Array.isArray(artifact?.holdout) && artifact.holdout.length > 0 &&
      typeof artifact?.ui_assertions?.path === "string" &&
      /^[a-f0-9]{64}$/i.test(artifact?.ui_assertions?.sha256 ?? "") &&
      artifact?.remote_fallback === false &&
      gates?.schema === true && gates?.manifest_identity === true &&
      gates?.provenance === true && gates?.holdout === true &&
      gates?.remote_fallback === true && gates?.rss === true &&
      gates?.safety === true && gates?.reproducibility === true && gates?.holdout_evaluation === true &&
      gates?.non_regression === true &&
      artifact?.comparison?.pass === true &&
      Number.isFinite(artifact?.comparison?.threshold) &&
      Number.isFinite(metrics?.tesseract?.records) && metrics.tesseract.records > 0 &&
      Number.isFinite(metrics?.production?.records) && metrics.production.records > 0 &&
      artifact?.results?.path && /^[a-f0-9]{64}$/i.test(artifact?.results?.sha256 ?? ""))) return rejected

     const base = dirname(path)
    const manifestPath = resolve(base, artifact.manifest.path)
    const resultsPath = resolve(base, artifact.results.path)
    const manifestBytes = readFileSync(manifestPath)
    if (createHash("sha256").update(manifestBytes).digest("hex") !== artifact.manifest.sha256) return rejected
    const manifest = JSON.parse(manifestBytes.toString("utf8"))
    if (manifest.schema_version !== 2 || manifest.provenance?.authorship !== "human" || manifest.provenance?.blinding !== "holdout" || !Array.isArray(manifest.holdout) || manifest.holdout.length === 0) return rejected
    const holdoutEvaluation = artifact.holdout_evaluation
    if (!holdoutEvaluation || typeof holdoutEvaluation.path !== "string" || !/^[a-f0-9]{64}$/i.test(holdoutEvaluation.sha256 ?? "")) return rejected
    const holdoutEvaluationPath = resolve(base, holdoutEvaluation.path)
    const holdoutEvaluationBytes = readFileSync(holdoutEvaluationPath)
    if (createHash("sha256").update(holdoutEvaluationBytes).digest("hex") !== holdoutEvaluation.sha256) return rejected
    const holdoutEvaluationArtifact = JSON.parse(holdoutEvaluationBytes.toString("utf8"))
    if (holdoutEvaluationArtifact.schema_version !== 2 || holdoutEvaluationArtifact.frozen !== true || holdoutEvaluationArtifact.split !== "holdout" || holdoutEvaluationArtifact.review !== "independent" || holdoutEvaluationArtifact.manifest_sha256 !== artifact.manifest.sha256 || holdoutEvaluationArtifact.candidate_model !== artifact.candidate_model || holdoutEvaluationArtifact.decision !== "independent-held-out-review" || !Array.isArray(holdoutEvaluationArtifact.holdout_ids) || JSON.stringify([...holdoutEvaluationArtifact.holdout_ids].sort()) !== JSON.stringify([...manifest.holdout].sort())) return rejected
    if (!holdoutEvaluationArtifact.results || typeof holdoutEvaluationArtifact.results.path !== "string" || !/^[a-f0-9]{64}$/i.test(holdoutEvaluationArtifact.results.sha256 ?? "") || !holdoutEvaluationArtifact.scorer || typeof holdoutEvaluationArtifact.scorer.path !== "string" || !/^[a-f0-9]{64}$/i.test(holdoutEvaluationArtifact.scorer.sha256 ?? "")) return rejected
    const evaluationResultsBytes = readFileSync(resolve(base, holdoutEvaluationArtifact.results.path))
    if (createHash("sha256").update(evaluationResultsBytes).digest("hex") !== holdoutEvaluationArtifact.results.sha256) return rejected
    const scorerBytes = readFileSync(resolve(base, holdoutEvaluationArtifact.scorer.path))
    if (createHash("sha256").update(scorerBytes).digest("hex") !== holdoutEvaluationArtifact.scorer.sha256) return rejected
     const scorer = JSON.parse(scorerBytes.toString("utf8"))
     if (canonical(scorer) !== canonical(FROZEN_SCORER)) return rejected
     if (!verifyAttestation(artifact, expectedHash, expectedCandidate ?? artifact.candidate_model, expectedBaseline ?? artifact.baseline_model, String(opts.configSha256 ?? ""), sha256(scorerBytes), opts)) return rejected
    const holdoutRecords = evaluationResultsBytes.toString("utf8").split("\n").filter(Boolean).map(JSON.parse)
    const holdoutSamples = new Map((manifest.corpus ?? []).filter((sample: any) => sample.split === "holdout").map((sample: any) => [sample.id, sample]))
    const means = new Map<string, number[]>()
    for (const record of holdoutRecords) {
      const sample = holdoutSamples.get(record.sample_id)
      if (!sample || record.schema_version !== 2 || record.split !== "holdout" || !["baseline", "candidate"].includes(record.role) || record.model !== (record.role === "candidate" ? artifact.candidate_model : artifact.baseline_model) || sample.fixture?.path !== record.fixture?.path || sample.fixture?.sha256 !== record.fixture?.sha256 || sample.oracle?.path !== record.oracle?.path || sample.oracle?.sha256 !== record.oracle?.sha256 || typeof record.raw_output !== "string" || record.raw_output_sha256 !== createHash("sha256").update(record.raw_output).digest("hex") || !Array.isArray(record.errors) || record.errors.length || record.timed_out === true || record.rss?.gate_pass !== true || !Number.isFinite(record.rss?.peak_aggregate_mb) || record.rss.peak_aggregate_mb * 1024 * 1024 >= 40 * 1024 * 1024 * 1024 || record.scores?.exact_value_safety !== true || record.scores?.span_safety !== true || record.scores?.ui_assertions?.pass !== true || record.scores?.pass !== true) return rejected
      const score = recomputedScore(record, base)
      if (!score || score.total < 0.8 || !score.exact_value_safety || !score.span_safety) return rejected
      means.set(record.role, [...(means.get(record.role) ?? []), score.total])
    }
    for (const sampleId of holdoutSamples.keys()) for (const role of ["baseline", "candidate"]) {
      const group = holdoutRecords.filter((record: any) => record.sample_id === sampleId && record.role === role)
      if (group.length < 2 || new Set(group.map((record: any) => record.raw_output_sha256)).size !== 1) return rejected
    }
    if (holdoutSamples.size === 0 || !means.get("baseline")?.length || !means.get("candidate")?.length) return rejected
    const mean = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length
    if (mean(means.get("candidate")!) < mean(means.get("baseline")!) * 0.95) return rejected
    if (!artifact.ui_assertions || JSON.stringify(artifact.ui_assertions) !== JSON.stringify(manifest.ui_assertions)) return rejected
    const uiPath = resolve(base, manifest.ui_assertions.path)
    if (createHash("sha256").update(readFileSync(uiPath)).digest("hex") !== manifest.ui_assertions.sha256) return rejected
    const ui = JSON.parse(readFileSync(uiPath).toString("utf8"))
    if (ui.schema_version !== 1 || ui.synthetic_text !== true || ui.no_image_file_parts !== true || ui.local_transport !== true || ui.remote_fallback !== false) return rejected
    const resultBytes = readFileSync(resultsPath)
    if (createHash("sha256").update(resultBytes).digest("hex") !== artifact.results.sha256) return rejected
    const samples = new Map((manifest.corpus ?? []).map((sample: any) => [sample.id, sample]))
    const fixtureHashes = new Map<string, { id: string; split: string }>()
    const oracleHashes = new Map<string, { id: string; split: string }>()
    for (const sample of manifest.corpus ?? []) {
      for (const [label, hash, seen] of [["fixture", sample.fixture?.sha256, fixtureHashes], ["oracle", sample.oracle?.sha256, oracleHashes]] as const) {
        if (typeof hash !== "string") return rejected
        const previous = seen.get(hash)
        if (previous && previous.id !== sample.id && (previous.split !== sample.split || sample.split === "holdout")) return rejected
        seen.set(hash, { id: sample.id, split: sample.split })
      }
    }
    const records = resultBytes.toString("utf8").split("\n").filter(Boolean).map(JSON.parse)
    if (!records.length || !records.some((r: any) => r.role === "apple") || !records.some((r: any) => r.role === "tesseract") || !records.some((r: any) => r.role === "production")) return rejected
    for (const record of records) {
      const sample = samples.get(record.sample_id)
      const expectedModel = record.role === "production"
        ? artifact.candidate_model
        : record.role === "tesseract"
          ? artifact.baseline_model
          : record.role === "apple"
            ? "extract.py:apple"
            : null
      if (record.schema_version !== 2 || record.split !== "benchmark" || !sample || sample.split !== "benchmark" || manifest.holdout.includes(record.sample_id) ||
        !expectedModel || record.model !== expectedModel ||
        sample.fixture?.path !== record.fixture?.path || sample.fixture?.sha256 !== record.fixture?.sha256 ||
        sample.oracle?.path !== record.oracle?.path || sample.oracle?.sha256 !== record.oracle?.sha256 ||
        typeof record.raw_output !== "string" || record.raw_output_sha256 !== createHash("sha256").update(record.raw_output).digest("hex") ||
        record.errors?.some((error: unknown) => /timeout/i.test(String(error))) || record.timed_out === true ||
        record.rss?.gate_pass !== true ||
        (record.scores?.exact_value_safety ?? record.scores?.authoritative?.exact_value_safety) !== true ||
        (record.scores?.span_safety ?? record.scores?.authoritative?.span_safety) !== true ||
        record.scores?.ui_assertions?.pass !== true || record.provenance?.production_enabled === true || !recomputedScore(record, base)) return rejected
    }
    const meanScore = (role: string) => {
      const roleRecords = records.filter((record: any) => record.role === role)
      const values = roleRecords.map((record: any) => recomputedScore(record, base)?.total ?? NaN)
      return values.length > 0 && values.every(Number.isFinite)
        ? values.reduce((sum: number, value: number) => sum + value, 0) / values.length
        : NaN
    }
    const baselineScore = meanScore("tesseract")
    const candidateScore = meanScore("production")
    if (!Number.isFinite(baselineScore) || !Number.isFinite(candidateScore) || candidateScore < baselineScore * 0.95) return rejected
    const repeated = new Map<string, any[]>()
    for (const record of records) {
      const key = `${record.sample_id}\0${record.role}`
      repeated.set(key, [...(repeated.get(key) ?? []), record])
    }
    for (const group of repeated.values()) {
      if (group.length < 2 || new Set(group.map(record => record.raw_output_sha256)).size !== 1 || group.some(record => record.errors?.length || record.timed_out === true)) return rejected
    }
     const capabilities = artifact.attestation.capabilities as string[]
     return {
       evidence: capabilities.includes("visionCandidate"),
       stickyNotes: capabilities.includes("stickyNotes"),
     }
  } catch {
    return rejected
  }
}

// Map media kind -> the skill that handles the richer, non-extraction work.
const SKILL_FOR: Record<string, string> = {
  pdf: "tool--pdf",
  audio: "tool--transcribe",
  video: "tool--transcribe",
  image: "tool--pdf", // ocr_extract lives in tool--pdf
}

function mimeMatches(mime: string, patterns: string[]): boolean {
  const m = (mime || "").toLowerCase()
  for (const p of patterns) {
    const pat = p.toLowerCase()
    if (pat.endsWith("/*")) {
      if (m.startsWith(pat.slice(0, -1))) return true // "audio/" prefix
    } else if (m === pat) {
      return true
    }
  }
  return false
}

function kindForMime(mime: string): string {
  const m = (mime || "").toLowerCase()
  if (m === "application/pdf") return "pdf"
  if (m.startsWith("audio/")) return "audio"
  if (m.startsWith("video/")) return "video"
  if (m.startsWith("image/")) return "image"
  if (m === "application/zip" || (m.startsWith("application/") && m.includes("zip"))) return "zip"
  return "unknown"
}

function looksLikeZip(part: any): boolean {
  const name = String(part?.filename || part?.url || "").toLowerCase()
  return name.endsWith(".zip")
}

const MAX_AGENT_FRAMES = 16

function hashStr(s: string): string {
  let hash = 5381
  for (let i = 0; i < s.length; i++) {
    hash = ((hash << 5) + hash) + s.charCodeAt(i)
  }
  return (hash >>> 0).toString(36)
}

function latestUserText(messages: any[]): string {
  try {
    if (!Array.isArray(messages)) return ""
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i]
      if (!msg) continue
      const role = msg.role ?? msg.info?.role
      if (role === "user") {
        const parts = msg.parts
        if (Array.isArray(parts)) {
          let text = ""
          for (const p of parts) {
            if (p && p.type === "text" && p.text && !p.synthetic) {
              text += p.text
            }
          }
          const trimmed = text.trim()
          return trimmed.slice(0, 2000)
        }
      }
    }
  } catch {
    // try/catch -> "" on any error
  }
  return ""
}

async function mapWithConcurrency<T>(items: T[], limit: number, worker: (item: T, index: number) => Promise<void>): Promise<void> {
  let next = 0
  const runners: Promise<void>[] = []
  const run = async () => {
    while (true) {
      const i = next++
      if (i >= items.length) return
      await worker(items[i], i)
    }
  }
  const n = Math.min(limit, items.length)
  for (let k = 0; k < n; k++) runners.push(run())
  await Promise.all(runners)
}

function imagePrompt(goal: string): string {
  let p = "Extract the attached media into a compact JSON digest for a downstream " +
    "agent that will NOT see the original file. Output ONLY minified JSON — no " +
    "prose, no code fences. Schema: " +
    "{\"content_type\":string,\"summary\":string,\"key_metadata\":{byte-exact ids/codes/amounts/dates/dimensions/names/urls},\"relevant_spans\":[{\"quote\":verbatim string,\"location\":short where-hint}],\"full_text\":string}. " +
    "Preserve exact strings; never guess."
  if (goal) {
    p += `\nThe downstream agent's current goal is:\n"""\n${goal}\n"""\n` +
      "Prioritize relevant_spans that answer this goal, while still filling all schema fields."
  }
  return p
}

function videoPrompt(goal: string, transcript: string): string {
  let tText = transcript || ""
  if (tText.trim() === "") {
    tText = "(no transcript / silent or audioless video)"
  } else if (tText.length > 12000) {
    tText = tText.slice(0, 12000) + "…[truncated]"
  }

  let p = "Extract the attached video into a compact JSON digest for a downstream " +
    "agent that will NOT see the original video. N ordered scene-sampled keyframes are " +
    "ATTACHED (in order) plus the audio transcript is inline. Output ONLY minified JSON — no " +
    "prose, no code fences. Schema: " +
    "{\"content_type\":\"video\",\"summary\":string,\"key_metadata\":{byte-exact values visible on-frame or spoken},\"timeline\":[{\"frame\":int(1-based),\"visual\":short desc,\"transcript_excerpt\":string|null}],\"relevant_spans\":[{\"quote\":verbatim,\"location\":\"frame N\"|\"transcript\"}],\"full_transcript_included\":bool}. " +
    "Preserve exact strings; never guess.\n\n" +
    `TRANSCRIPT:\n${tText}`

  if (goal) {
    p += `\n\nThe downstream agent's current goal is:\n"""\n${goal}\n"""\n` +
      "Prioritize relevant_spans that answer this goal, while still filling all schema fields."
  }
  return p
}

export const MediaGuardPlugin: Plugin = async ({ $, client }, opts: Options = {}) => {
  const emit = async (message: string, level: "debug"|"info"|"warn"|"error" = "info", extra?: Record<string, unknown>) => {
    try { await client?.app?.log({ body: { service: "media-guard", level, message, ...(extra ? { extra } : {}) } }) } catch {}
  }

  const agentKinds = new Set(opts.agentKinds ?? ["image"])
  const configuredBaselineModel = opts.visionModel ?? "gemma4:12b"
  const configuredCandidateModel = opts.visionCandidateModel ?? "qwen2.5vl:7b"
  const admission = verifiedEvidence(opts.evidencePath, opts.evidenceSha256, configuredCandidateModel, configuredBaselineModel, opts)
  const evidenceVerified = admission.evidence
  const candidateRequested = opts.visionCandidateEnabled === true
  const candidateEnabled = candidateRequested && evidenceVerified
  const forceDeterministicOnRejectedCandidate = candidateRequested && !evidenceVerified
  const stickyNotesEnabled = candidateEnabled && admission.stickyNotes
  const visionEnabled = opts.visionEnabled !== false
  const visionModel = candidateEnabled
    ? configuredCandidateModel
    : configuredBaselineModel
  const visionBaseUrl = (opts.visionBaseUrl ?? "http://127.0.0.1:11434").replace(/\/+$/, "")
  const visionNumCtx = opts.visionNumCtx ?? 16384
  const visionTimeoutMs = (opts.visionTimeoutSec ?? 120) * 1000

  const mimes = [...(opts.mimes ?? DEFAULT_MIMES)]
  if (opts.ocrImages || agentKinds.has("image")) {
    if (!mimes.includes("image/*")) {
      mimes.push("image/*")
    }
  }
  for (const k of agentKinds) {
    let p: string | null = null
    if (k === "audio") p = "audio/*"
    else if (k === "video") p = "video/*"
    else if (k === "pdf") p = "application/pdf"
    if (p && !mimes.includes(p)) {
      mimes.push(p)
    }
  }
  for (const z of ZIP_MIMES) {
    if (!mimes.includes(z)) mimes.push(z)
  }

  const maxChars = opts.maxChars ?? 60_000
  const model = opts.model ?? "base"
  const timeoutSec = opts.timeoutSec ?? 180
  const concurrency = Math.max(1, opts.concurrency ?? 6)
  const batchThreshold = opts.batchThreshold ?? 24
  const transformBudgetMs = (opts.transformBudgetSec ?? 240) * 1000
  const ocrCorrection = opts.ocrCorrection === true && evidenceVerified
  const extractorCache = opts.extractorCache !== false

  // in-memory result cache: path+mtime+size -> note text
  const cache = new Map<string, string>()
  const MAX_CACHE = 512

  // Resolve a FilePart's url/source to a local file path.
  const resolvePath = (part: any): string | null => {
    const src = part?.source
    if (src && typeof src.path === "string" && src.path) return src.path
    const url: string = part?.url ?? ""
    if (!url) return null
    if (url.startsWith("file://")) {
      try { return decodeURIComponent(new URL(url).pathname) } catch { return null }
    }
    if (url.startsWith("data:")) {
      const comma = url.indexOf(",")
      if (comma < 0) return null
      const meta = url.slice(5, comma)
      const body = url.slice(comma + 1)
      if (!/base64/i.test(meta)) return null
      try {
        const buf = Buffer.from(body, "base64")
        const h = createHash("sha256").update(buf).digest("hex").slice(0, 32)
        const fn = part?.filename || ""
        const pMime = part?.mime || meta
        const ext = fn.includes(".") ? fn.slice(fn.lastIndexOf(".")).toLowerCase()
          : pMime.includes("png") ? ".png"
          : pMime.includes("jpeg") || pMime.includes("jpg") ? ".jpg"
          : pMime.includes("pdf") ? ".pdf"
          : pMime.includes("zip") ? ".zip"
          : pMime.includes("audio") ? ".bin"
          : pMime.includes("video") ? ".bin"
          : ".bin"
        const p = join(DATAURL_DIR, h + ext)
        if (!existsSync(p) || statSync(p).size !== buf.length) {
          mkdirSync(DATAURL_DIR, { recursive: true })
          writeFileSync(p, buf)
        }
        return p
      } catch { return null }
    }
    if (url.startsWith("/")) return url
    return null // http(s) or anything not locally reachable
  }

  const uploadSize = (part: any): string => {
    const path = resolvePath(part)
    if (!path) return "size unavailable"
    try {
      const bytes = statSync(path).size
      const units = ["B", "KiB", "MiB", "GiB"]
      let value = bytes
      let unit = 0
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024
        unit++
      }
      const formatted = value.toFixed(value >= 10 ? 0 : 1).replace(/\.0$/, "")
      const human = unit === 0 ? `${bytes} B` : `${formatted} ${units[unit]}`
      return `${human}, ${bytes} bytes`
    } catch {
      return "size unavailable"
    }
  }

  // Call extract.py; returns parsed JSON or null on hard failure.
  const runExtract = async (path: string, mime: string, max: number, ocrEngine?: string, classifyOnly = false, deadline = Infinity): Promise<any | null> => {
    if (Number.isFinite(deadline) && Date.now() >= deadline) return null
    const remainingSec = Number.isFinite(deadline) ? (deadline - Date.now()) / 1000 : timeoutSec
    const args = ["python3", EXTRACT, path, "--mime", mime, "--max-chars", String(max), "--timeout", String(Math.min(timeoutSec, remainingSec)), "--model", model]
    if (ocrEngine) args.push("--ocr-engine", ocrEngine)
    if (classifyOnly) args.push("--classify-only")
    if (ocrCorrection) args.push("--ocr-correction")
    if (!extractorCache) args.push("--no-cache")
    const r = await $`${args}`
      .quiet().nothrow()
    const out = String(r.stdout).trim()
    if (!out) return null
    try { return JSON.parse(out) } catch { return null }
  }

  const skillHint = (kind: string): string => {
    const s = SKILL_FOR[kind]
    return s ? `Load the \`${s}\` skill to process it.` : "No local extractor is available for this file."
  }

  const extractJson = (raw: string): string | null => {
    let content = raw.trim()
    const fenceMatch = content.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)
    if (fenceMatch) {
      content = fenceMatch[1]
    } else {
      const firstBrace = content.indexOf("{")
      const lastBrace = content.lastIndexOf("}")
      if (firstBrace !== -1 && lastBrace !== -1 && lastBrace >= firstBrace) {
        content = content.slice(firstBrace, lastBrace + 1)
      }
    }
    try {
      return JSON.stringify(JSON.parse(content))
    } catch {
      return null
    }
  }

  const runLocalVision = async (paths: string[], promptText: string): Promise<string | null> => {
    if (!visionEnabled) return null
    const images: string[] = []
    for (const p of paths) {
      try {
        images.push(Buffer.from(readFileSync(p)).toString("base64"))
      } catch {
        // skip unreadable files
      }
    }
    if (images.length === 0) return null
    try {
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), visionTimeoutMs)
      const resp = await fetch(`${visionBaseUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: visionModel,
          messages: [{ role: "user", content: promptText, images }],
          stream: false,
          think: false,
          format: "json",
          options: { temperature: 0, num_ctx: visionNumCtx },
        }),
        signal: controller.signal,
      })
      clearTimeout(timer)
      if (!resp.ok) {
        if (process.env.MEDIA_GUARD_DEBUG) console.error("[media-guard:vision] miss (fallback to deterministic local extraction)")
        return null
      }
      const json: any = await resp.json()
      const content: string | undefined = json?.message?.content
      if (!content) {
        if (process.env.MEDIA_GUARD_DEBUG) console.error("[media-guard:vision] miss (fallback to deterministic local extraction)")
        return null
      }
      if (process.env.MEDIA_GUARD_DEBUG) console.error("[media-guard:vision] ok model=" + visionModel + " images=" + images.length)
      return content
    } catch {
      if (process.env.MEDIA_GUARD_DEBUG) console.error("[media-guard:vision] miss (fallback to deterministic local extraction)")
      return null
    }
  }

  const extractNote = (res: any, kind: string, path: string, header: string): string => {
    if (res && res.status === "ok" && res.text) {
      const trunc = res.truncated
        ? `\n\n…[truncated to ${maxChars} chars of ${res.full_chars}. For the full ` +
          `content use the media_extract tool or load the \`${SKILL_FOR[kind] ?? "relevant"}\` skill on ${path}.]`
        : ""
      const via = res.tool ? ` via ${res.tool}` : ""
      const original = typeof res.original_text === "string" ? res.original_text : res.text
      const corrected = typeof res.corrected_text === "string" ? res.corrected_text : null
      const alternative = corrected ? `\n\n----- BEGIN CORRECTED ALTERNATIVE (NOT AUTHORITATIVE) -----\n${corrected}\n----- END CORRECTED ALTERNATIVE -----` : ""
      return `${header}\nIts extracted text is inlined below${via}.\nFile: ${path}\n\n` +
        `----- BEGIN AUTHORITATIVE ORIGINAL ${kind.toUpperCase()} OCR/TEXT -----\n${original}` +
        `\n----- END AUTHORITATIVE ORIGINAL ${kind.toUpperCase()} OCR/TEXT -----${alternative}${trunc}`
    } else {
      const why = res?.detail ? ` (${res.detail})` : ""
      return `${header}\nLocal extraction was unavailable${why}. ${skillHint(kind)} ` +
        `File: ${path}`
    }
  }

  // Build the replacement text for one intercepted file part.
  // ctl.forceDeterministic — skip agent processing, use local extract only.
  // ctl.deadline — wall-clock expiry; if exceeded (after cache check) return a fast pointer note.
  // ctl.counters — optional debug counters incremented at each return path.
  // ctl.sink — optional sink to tag which path handled the file.
  const buildNote = async (part: any, mime: string, userGoal: string, ctl: { forceDeterministic: boolean; deadline: number; fileDeadline?: number; counters?: { agent: number; deterministic: number; pointer: number; error: number }; sink?: { via?: string }; depth?: number } = { forceDeterministic: false, deadline: Infinity }): Promise<string> => {
    const kind = kindForMime(mime)
    const path = resolvePath(part)
    const label = part?.filename || path || part?.url || "attachment"
    const viaAgent = agentKinds.has(kind) && !ctl.forceDeterministic
    const initialHeader = viaAgent
      ? `[media-guard] A ${kind} file ("${label}") was processed by a cheaper model instead of being sent to you as raw bytes (saves tokens; byte-exact values are preserved in key_metadata below).`
      : `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the ` +
        `model — this provider rejects ${mime} file parts (sending it would break the turn).`

    const win = (v: string) => { if (ctl.sink) ctl.sink.via = v }

    if (!path) {
      if (ctl.counters) ctl.counters.pointer++
      win("unreachable")
      return `${initialHeader}\nThe file isn't reachable as a local path, so its content ` +
        `couldn't be extracted automatically. ${skillHint(kind)}`
    }

    // in-memory cache
    let ckey = path
    try { const st = statSync(path); ckey = `${path}:${st.mtimeMs}:${st.size}:${maxChars}:${model}:${mime}` } catch {}
    if (viaAgent) {
      ckey += `:${visionModel}:${hashStr(userGoal)}`
    }
    const hit = cache.get(ckey)
    if (hit) return hit

    const fileDeadline = ctl.fileDeadline ?? ctl.deadline

    // deadline guard — cached results always return; fresh work honours budget
    if (Date.now() >= fileDeadline) {
      if (ctl.counters) ctl.counters.pointer++
      win("skipped-budget")
      return `${initialHeader}\nThe per-turn media budget was exhausted before this file could be processed, so it was NOT sent to the model. ${skillHint(kind)} File: ${path}`
    }

    const cacheSetAndEvict = (key: string, note: string): string => {
      cache.set(key, note)
      if (cache.size > MAX_CACHE) {
        cache.delete(cache.keys().next().value as string)
      }
      return note
    }

    // ZIP / archive branch — decompress locally, then run each extracted file
    // back through this SAME media-guard pipeline (recursively, depth-capped).
    if (kind === "zip") {
      const depth = ctl.depth ?? 0
      const res = await runExtract(path, "application/zip", maxChars)
      const files: any[] = Array.isArray(res?.meta?.files) ? res.meta.files : []
      const listing = String(res?.text || "").trim()
      if (files.length === 0) {
        if (ctl.counters) ctl.counters.pointer++
        win("zip-empty")
        const why = res?.detail ? ` (${res.detail})` : ""
        return cacheSetAndEvict(ckey, `[media-guard] A zip archive ("${label}") was attached but no extractable files were found${why}. It was NOT sent to the model. File: ${path}`)
      }
      const dest = res?.meta?.dest ?? dirname(files[0].path)
      const zipHeader = `[media-guard] A zip archive ("${label}") was attached but NOT sent to the model as raw bytes. It was decompressed locally and each of its ${files.length} file(s) was processed by media-guard (same pipeline). Archive dir: ${dest}`
      if (depth >= MAX_ZIP_DEPTH) {
        if (ctl.counters) ctl.counters.pointer++
        win("zip-maxdepth")
        return cacheSetAndEvict(ckey, `${zipHeader}\nNested-archive depth limit (${MAX_ZIP_DEPTH}) reached — inner archive entries were left on disk, not expanded.\n\n${listing}`)
      }
      const childNotes: string[] = new Array(files.length)
      await mapWithConcurrency(files, concurrency, async (f: any, idx: number) => {
        const childName = f.name || (f.path ? String(f.path).split("/").pop() : "entry")
        const childPart = { filename: childName, url: `file://${f.path}`, source: { path: f.path } }
        const childMime = f.mime || ""
        const childSink: { via?: string } = {}
        let note: string
        try {
         note = await buildNote(childPart, childMime, userGoal, { forceDeterministic: ctl.forceDeterministic, deadline: ctl.deadline, fileDeadline, counters: ctl.counters, sink: childSink, depth: depth + 1 })
        } catch (e) {
          note = `[media-guard] Zip entry "${childName}" could not be processed (${e instanceof Error ? e.message : String(e)}).`
        }
        childNotes[idx] = `===== ZIP ENTRY ${idx + 1}/${files.length}: ${childName} [${f.kind || "?"}] (via ${childSink.via ?? "unknown"}) =====\n${note}`
      })
      win("zip")
      const combined = `${zipHeader}\n\n${listing}\n\n` + childNotes.join("\n\n")
      return cacheSetAndEvict(ckey, combined)
    }

    // IMAGE agent branch (gated by forceDeterministic)
    if (kind === "image" && agentKinds.has("image") && !ctl.forceDeterministic && path) {
        const stickyClassification = await runExtract(path, mime, maxChars, "sticky-notes", true, fileDeadline)
        const stickyLabel = stickyClassification?.meta?.classification?.label
        const stickyRegions = stickyClassification?.meta?.note_regions
        if (stickyNotesEnabled && (stickyLabel === "sticky_notes" || (Array.isArray(stickyRegions) && stickyRegions.length > 0))) {
          const res = await runExtract(path, mime, maxChars, "sticky-notes", false, fileDeadline)
          if (ctl.counters) ctl.counters.agent++
          const note = extractNote(res, kind, path, `[media-guard] Sticky notes were routed to the local structured vision/OCR pipeline; raw image bytes were not sent.`)
          win("sticky-notes")
          return cacheSetAndEvict(ckey, note)
        }
        const classification = await runExtract(path, mime, maxChars, undefined, true, fileDeadline)
        const label = classification?.meta?.classification?.label
        const isDocument = classification?.meta?.document_image === true || label === "document" || label === "handwriting"
        if (isDocument) {
         const fallbackHeader = `[media-guard] A detected document image ("${label}") was OCR'd locally before model processing; raw image bytes were not sent.`
         const res = await runExtract(path, mime, maxChars, "production", false, fileDeadline)
         if (ctl.counters) ctl.counters.deterministic++
         const note = extractNote(res, kind, path, fallbackHeader)
         win("ocr-document")
         return cacheSetAndEvict(ckey, note)
       }
        const prompt = imagePrompt(userGoal)
       let digest = visionEnabled ? await runLocalVision([path], prompt) : null
       const json = digest ? extractJson(digest) : null
       if (json !== null) {
         if (ctl.counters) ctl.counters.agent++
         const header = `[media-guard] An image ("${label}") was distilled to a JSON digest by a cheaper model instead of being sent as raw bytes (byte-exact values preserved in key_metadata). (local vision)`
        const note = header + `\nDownstream Agent: Use the exact 'File: ${path}' path to read/reference this file or get the text directly from the 'full_text' field inside the JSON digest below. DO NOT attempt to run any re-OCR on the image and do not report that you cannot locate or see the image.\nFile: ${path}\n\n----- BEGIN MEDIA DIGEST (JSON) -----\n${json}\n----- END MEDIA DIGEST (JSON) -----`
        win("local-vision")
        return cacheSetAndEvict(ckey, note)
      }
      // On failure fall through to deterministic OCR
      const fallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
       const res = await runExtract(path, mime, maxChars, "tesseract", false, fileDeadline)
      if (ctl.counters) ctl.counters.deterministic++
      const note = extractNote(res, kind, path, fallbackHeader)
      win("ocr")
      return cacheSetAndEvict(ckey, note)
    }

    // VIDEO branch (gated by forceDeterministic)
    if (kind === "video" && agentKinds.has("video") && !ctl.forceDeterministic && path) {
       const res = await runExtract(path, mime, maxChars, undefined, false, fileDeadline) // frames + transcript
      const frames = Array.isArray(res?.meta?.frames) ? res.meta.frames : []
      const transcript = (res?.text || "")
      if (frames.length) {
         const useFrames = frames.slice(0, MAX_AGENT_FRAMES)
         const prompt = videoPrompt(userGoal, transcript)
         let digest = visionEnabled ? await runLocalVision(useFrames, prompt) : null
         const json = digest ? extractJson(digest) : null
         if (json !== null) {
           if (ctl.counters) ctl.counters.agent++
           const header = `[media-guard] A video ("${label}") was distilled by a cheaper multimodal model — ${frames.length} scene-sampled keyframes fused with its audio transcript — instead of being sent as raw bytes. Full transcript, frames, and the original video remain on disk. (local vision)`
          const note = header + `\nUse the media_extract tool on the File path for the COMPLETE transcript, or read individual frames.\nFile: ${path}\nFrames dir: ${dirname(useFrames[0])}\n\n----- BEGIN VIDEO DIGEST (JSON) -----\n${json}\n----- END VIDEO DIGEST (JSON) -----`
          win("local-vision")
          return cacheSetAndEvict(ckey, note)
        }
      }
      // fusion unavailable → fall back to deterministic transcript inline:
      const videoFallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
      if (ctl.counters) ctl.counters.deterministic++
      const note = extractNote(res, kind, path, videoFallbackHeader)
      win("extract")
      return cacheSetAndEvict(ckey, note)
    }

    // Otherwise (pdf / audio / non-agent kinds / forced-deterministic image or video)
    const fallbackHeader = `[media-guard] A ${kind} file ("${label}") was attached but NOT sent to the model — this provider rejects ${mime} file parts (sending it would break the turn).`
     const res = await runExtract(path, mime, maxChars, undefined, false, fileDeadline)
    if (ctl.counters) ctl.counters.deterministic++
    const note = extractNote(res, kind, path, fallbackHeader)
    win("extract")
    return cacheSetAndEvict(ckey, note)
  }

  // Convert every matched media file part in `parts` to a synthetic text part, in place.
  // `parts` is the live array reference. Fail-safe: every match is replaced even on error.
  const replaceMediaParts = async (
    matches: Array<{ parts: any[]; index: number; part: any; mime: string }>,
    userGoal: string,
    counters: { agent: number; deterministic: number; pointer: number; error: number },
    rejectedCandidate = false,
  ): Promise<{ forceDeterministic: boolean; elapsedMs: number }> => {
    const forceDeterministic = rejectedCandidate || matches.length > batchThreshold
    const deadline = Date.now() + transformBudgetMs
    const start = Date.now()
    const uploadSummary = matches.map((m, i) => {
      const filename = String(m.part?.filename || `attachment-${i + 1}`)
      return `${filename} (${uploadSize(m.part)})`
    }).join(", ")
    await emit(`received ${matches.length} upload(s): ${uploadSummary}; extracting locally before the model runs`, "info", { count: matches.length, concurrency, forceDeterministic })
    await mapWithConcurrency(matches, concurrency, async (m, i) => {
      const label = m.part?.filename || "attachment"
      const sink: { via?: string } = {}
      let text: string
      try {
        const fileDeadline = Math.min(deadline, Date.now() + timeoutSec * 1000)
        text = await buildNote(m.part, m.mime, userGoal, { forceDeterministic, deadline, fileDeadline, counters, sink })
      } catch (e) {
        counters.error++
        text = `[media-guard] A media file ("${label}", ${m.mime}) was attached but its content could not be processed (${e instanceof Error ? e.message : String(e)}). It was NOT sent to the model, to avoid leaking raw bytes or breaking the turn. If you need its content, use the media_extract tool on its local path.`
      }
      try {
        m.parts[m.index] = { id: m.part.id, sessionID: m.part.sessionID, messageID: m.part.messageID, type: "text", text, synthetic: true }
      } catch (e) { console.error("[media-guard] failed to swap media part:", e) }
    })
    const elapsedMs = Date.now() - start
    await emit(`media distillation complete in ${elapsedMs}ms`, "info", { ...counters })
    return { forceDeterministic, elapsedMs }
  }

  return {
    "chat.message": async (_input, output) => {
      try {
        const parts = output?.parts
        if (!Array.isArray(parts) || parts.length === 0) return
        let userGoal = ""
        try { for (const p of parts) if (p?.type === "text" && p.text && !p.synthetic) userGoal += p.text; userGoal = userGoal.trim().slice(0, 2000) } catch {}
        const matches: Array<{ parts: any[]; index: number; part: any; mime: string }> = []
        for (let i = 0; i < parts.length; i++) {
          const p = parts[i]
          let isMatch = false
          const zipByName = (() => { try { return looksLikeZip(p) } catch { return false } })()
          try { isMatch = p?.type === "file" && ((typeof p.mime === "string" && mimeMatches(p.mime, mimes)) || zipByName) } catch { isMatch = false }
          const matchMime = zipByName ? "application/zip" : p.mime
          if (isMatch) matches.push({ parts, index: i, part: p, mime: matchMime })
        }
        if (matches.length === 0) return
        const counters = { agent: 0, deterministic: 0, pointer: 0, error: 0 }
        const { forceDeterministic, elapsedMs } = await replaceMediaParts(matches, userGoal, counters, forceDeterministicOnRejectedCandidate)
        if (process.env.MEDIA_GUARD_DEBUG) {
          console.error("[media-guard:chat.message] " + JSON.stringify({ matches: matches.length, forceDeterministic, concurrency, batchThreshold, elapsedMs, budgetMs: transformBudgetMs, ...counters }))
        }
      } catch (e) {
        // absolute fail-safe: on ANY unexpected error, blank out matched image parts so image.normalize can't crash the turn
        try {
          const parts = output?.parts
          if (Array.isArray(parts)) {
            for (let i = 0; i < parts.length; i++) {
              const p = parts[i]
              if (p?.type === "file" && typeof p.mime === "string" && p.mime.startsWith("image/")) {
                parts[i] = { id: p.id, sessionID: p.sessionID, messageID: p.messageID, type: "text", text: `[media-guard] An image ("${p.filename || "image"}") was attached but could not be processed (${e instanceof Error ? e.message : String(e)}); it was converted to this note to keep the turn alive.`, synthetic: true }
              }
            }
          }
        } catch {}
      }
    },

    "experimental.chat.messages.transform": async (_input, output) => {
      let userGoal = ""
      try { userGoal = latestUserText(output.messages) } catch { userGoal = "" }

      // Phase 1: collect ALL matching file-part locations across all messages
      const matches: Array<{ msg: any; parts: any[]; index: number; part: any; mime: string }> = []
      for (const msg of output.messages ?? []) {
        let parts: any[]
        try {
          parts = msg.parts as any[]
          if (!Array.isArray(parts) || parts.length === 0) continue
        } catch { continue }
        for (let i = 0; i < parts.length; i++) {
          const p = parts[i]
          let isMatch = false
          const zipByName = (() => { try { return looksLikeZip(p) } catch { return false } })()
          try {
            isMatch = p?.type === "file" && ((typeof p.mime === "string" && mimeMatches(p.mime, mimes)) || zipByName)
          } catch { isMatch = false }
          if (!isMatch) continue
          const matchMime = zipByName ? "application/zip" : p.mime
          matches.push({ msg, parts, index: i, part: p, mime: matchMime })
        }
      }

      // Phase 2: process with replaceMediaParts
      const counters = { agent: 0, deterministic: 0, pointer: 0, error: 0 }
      const { forceDeterministic, elapsedMs } = await replaceMediaParts(matches, userGoal, counters, forceDeterministicOnRejectedCandidate)

      // Phase 3: Reassign parts to trigger reactivity on touched messages
      const touchedMessages = new Set<any>()
      for (const m of matches) touchedMessages.add(m.msg)
      for (const msg of touchedMessages) {
        try { msg.parts = msg.parts } catch (e) { console.error("[media-guard] failed to assign parts:", e) }
      }

      if (process.env.MEDIA_GUARD_DEBUG) {
        console.error("[media-guard] " + JSON.stringify({
          matches: matches.length,
          forceDeterministic,
          concurrency,
          batchThreshold,
          elapsedMs,
          budgetMs: transformBudgetMs,
          agent: counters.agent,
          deterministic: counters.deterministic,
          pointer: counters.pointer,
          error: counters.error,
        }))
      }

    },

    tool: {
      media_extract: tool({
        description:
          "Extract text from a local media file (PDF, audio, video, or image) " +
          "using deterministic local tools (PyMuPDF/pdftotext/pypdf, whisper, " +
          "tesseract). Use for on-demand parsing, a larger transcription model, " +
          "or to get the full untruncated text of an attachment. Returns the " +
          "extracted text.",
        args: {
          path: tool.schema.string().describe("Absolute path to the media file."),
          mime: tool.schema.string().optional()
            .describe("Optional mime hint, e.g. application/pdf, audio/mpeg."),
          maxChars: tool.schema.number().int().min(1000).max(2_000_000).default(200_000)
            .describe("Max characters of text to return."),
          model: tool.schema.string().optional()
            .describe("Whisper model for audio/video (tiny|base|small|medium|large). Default base."),
        },
        async execute({ path, mime, maxChars: mc, model: md }) {
          const args = ["python3", EXTRACT, path, "--mime", mime ?? "", "--max-chars", String(mc), "--timeout", String(Math.max(timeoutSec, 600)), "--model", md ?? model, "--ocr-engine", "production"]
          if (ocrCorrection) args.push("--ocr-correction")
          if (!extractorCache) args.push("--no-cache")
          const r = await $`${args}`
            .quiet().nothrow()
          const out = String(r.stdout).trim()
          if (!out) return "media_extract failed: no output from extractor."
          let res: any
          try { res = JSON.parse(out) } catch { return "media_extract failed to parse output:\n" + out.slice(0, 500) }
          if (res.status === "ok") {
            const t = res.truncated ? `\n[truncated to ${mc} of ${res.full_chars} chars]` : ""
            const original = typeof res.original_text === "string" ? res.original_text : res.text
            const corrected = typeof res.corrected_text === "string" ? res.corrected_text : null
            const alternative = corrected ? `\n\n----- CORRECTED ALTERNATIVE (NOT AUTHORITATIVE) -----\n${corrected}\n----- END CORRECTED ALTERNATIVE -----` : ""
            return `kind=${res.kind} tool=${res.tool} chars=${res.chars}${t}\n\n----- AUTHORITATIVE ORIGINAL OCR/TEXT -----\n${original}\n----- END AUTHORITATIVE ORIGINAL OCR/TEXT -----${alternative}`
          }
          return `media_extract status=${res.status}: ${res.detail ?? "unavailable"}`
        },
      }),
    },
  }
}

export default MediaGuardPlugin
