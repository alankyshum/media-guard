#!/usr/bin/env bun
/** Real anydoc integration probe: documents are generated locally, then materialized and preprocessed. */
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { MediaGuardPlugin } from "../media-guard.ts"

const root = mkdtempSync(join(tmpdir(), "anydoc-plugin-e2e-"))
const required = ["docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf", "epub", "csv"].map(ext => `quarterly-report.${ext}`)
let fixtures = process.env.ANYDOC_REAL_FIXTURES ?? "/tmp/anydoc-real"
const assert = (value: unknown, message: string) => { if (!value) throw new Error(message) }
const partsText = (output: any) => output.parts.filter((p: any) => typeof p.text === "string").map((p: any) => p.text).join("\n")
const attachment = (id: string, filename: string, mime: string, path: string) => ({ id, type: "file", filename, mime, source: { path } })
const file = (name: string) => join(fixtures, name)
const mimes: Record<string, string> = { docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation", odt: "application/vnd.oasis.opendocument.text", ods: "application/vnd.oasis.opendocument.spreadsheet", odp: "application/vnd.oasis.opendocument.presentation", rtf: "application/rtf", epub: "application/epub+zip", csv: "text/csv" }
const output = (result: ReturnType<typeof Bun.spawnSync>) => `${result.stdout}`.trim() || `${result.stderr}`.trim()
function usablePython(python: string): boolean { return Bun.spawnSync([python, "-c", "import docx,openpyxl,pptx,odf"], { stdout: "pipe", stderr: "pipe" }).exitCode === 0 }
function bootstrap(): boolean {
  if (required.every(name => existsSync(join(fixtures, name)))) return true
  fixtures = join(root, "fixtures")
  const cached = join(tmpdir(), "anydoc-real-fixtures-venv", "bin", "python")
  const candidates = [process.env.MEDIA_GUARD_ANYDOC_FIXTURE_PYTHON, join(import.meta.dir, "..", ".venv", "bin", "python"), "python3"].filter((p): p is string => !!p)
  let python = candidates.find(usablePython)
  if (!python) {
    const venv = join(tmpdir(), "anydoc-real-fixtures-venv")
    const made = Bun.spawnSync(["python3", "-m", "venv", venv], { stdout: "pipe", stderr: "pipe" })
    if (made.exitCode !== 0) { console.log(`SKIP anydoc E2E: fixture dependencies unavailable (${output(made)})`); return false }
    const installed = Bun.spawnSync([cached, "-m", "pip", "install", "python-docx", "openpyxl", "python-pptx", "odfpy"], { stdout: "pipe", stderr: "pipe", timeout: 300000 })
    if (installed.exitCode !== 0 || !usablePython(cached)) { console.log(`SKIP anydoc E2E: fixture dependencies unavailable (${output(installed)})`); return false }
    python = cached
  }
  const generated = Bun.spawnSync([python, join(import.meta.dir, "generate-anydoc-real-fixtures.py"), fixtures], { stdout: "pipe", stderr: "pipe", timeout: 300000 })
  if (generated.exitCode !== 0 || !required.every(name => existsSync(join(fixtures, name)))) throw new Error(`fixture generation failed: ${output(generated)}`)
  console.log(`PASS fixtures: generated locally with ${python}`)
  return true
}
function encryptedFixture(): string | null {
  const target = join(fixtures, "quarterly-report-encrypted.docx")
  let python = [process.env.MEDIA_GUARD_ANYDOC_FIXTURE_PYTHON, join(import.meta.dir, "..", ".venv", "bin", "python"), join(tmpdir(), "anydoc-real-fixtures-venv", "bin", "python"), "python3"].filter((p): p is string => !!p).find(p => Bun.spawnSync([p, "-c", "import msoffcrypto"], { stdout: "ignore", stderr: "ignore" }).exitCode === 0)
  if (!python) {
    const cached = join(tmpdir(), "anydoc-real-fixtures-venv", "bin", "python")
    const installed = existsSync(cached) && Bun.spawnSync([cached, "-m", "pip", "install", "msoffcrypto-tool"], { stdout: "pipe", stderr: "pipe", timeout: 300000 })
    if (!installed || installed.exitCode !== 0 || Bun.spawnSync([cached, "-c", "import msoffcrypto"], { stdout: "ignore", stderr: "ignore" }).exitCode !== 0) { console.log(`SKIP encrypted document: msoffcrypto-tool unavailable${installed ? ` (${output(installed)})` : ""}`); return null }
    python = cached
  }
  const made = Bun.spawnSync([python, join(import.meta.dir, "generate-encrypted-anydoc-fixture.py"), file("quarterly-report.docx"), target], { stdout: "pipe", stderr: "pipe", timeout: 300000 })
  if (made.exitCode !== 0) { console.log(`SKIP encrypted document: ${output(made)}`); return null }
  return target
}
async function pipeline(part: any, name: string) {
  const plugin = await MediaGuardPlugin({}, { materializationDir: join(root, `${name}-stage`), cacheDir: join(root, `${name}-cache`), timeoutMs: 300000 })
  const output = { parts: [{ type: "text", text: "Please inspect attachment." }, part] }
  await plugin["chat.message"]!({}, output)
  return partsText(output)
}
try {
  if (!bootstrap()) process.exitCode = 0
  else {
  const formats = ["docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf", "epub"]
  for (const ext of formats) {
    const name = `quarterly-report.${ext}`; assert(existsSync(file(name)), `missing fixture ${name}`)
    const output = await pipeline(attachment(ext, name, mimes[ext], file(name)), ext)
    assert(output.includes("[media-preprocess extracted: kind=document"), `${ext}: document extraction marker missing: ${output}`)
    assert(output.includes("Quarterly Report") && output.includes("MARKER-BOLD-7391"), `${ext}: known document markers missing: ${output}`)
    if (ext === "epub") assert(!output.includes("[media-preprocess archive:"), "epub was incorrectly treated as archive")
    console.log(`PASS ${ext}: real document materialized and extracted`)
  }
  const zip = join(root, "bundle.zip")
  const zipped = Bun.spawnSync(["zip", "-q", zip, file("quarterly-report.docx"), file("quarterly-report.xlsx")]); assert(zipped.exitCode === 0, `zip failed: ${zipped.stderr}`)
  const archive = await pipeline(attachment("zip", "bundle.zip", "application/zip", zip), "archive")
  assert(archive.includes('"kind":"document"') && archive.includes('"handling":"auto-preprocessed"'), `ZIP document member manifest missing: ${archive}`)
  assert(archive.includes("MARKER-BOLD-7391"), `ZIP real anydoc text missing: ${archive}`)
  console.log("PASS zip: real docx/xlsx members auto-preprocessed through anydoc")
  const csv = await pipeline(attachment("csv", "quarterly-report.csv", "text/csv", file("quarterly-report.csv")), "csv")
  assert(csv.includes("[media-preprocess text-file:") && !csv.includes("kind=document"), `CSV was not kept as text: ${csv}`)
  console.log("PASS csv: classified as text, not document")
  const corrupt = join(root, "corrupt.docx"); writeFileSync(corrupt, Buffer.from("not a docx\x00random bytes"))
  const bad = await pipeline(attachment("bad", "corrupt.docx", mimes.docx, corrupt), "corrupt")
  assert(bad.includes("[media-preprocess failed: kind=document") || bad.includes("[media-preprocess uncertain: kind=document"), `corrupt docx was not contained: ${bad}`)
  console.log("PASS corrupt docx: extraction failure contained in transform")
  const encrypted = encryptedFixture()
  if (encrypted) {
    const direct = Bun.spawnSync(["npx", "-y", "@firecrawl/anydoc", encrypted], { stdout: "pipe", stderr: "pipe", timeout: 300000 })
    const directErr = `${direct.stderr}`
    assert(direct.exitCode !== 0 && /anydoc:.*encrypt/i.test(directErr), `encrypted direct anydoc did not fail clearly: exit=${direct.exitCode} stderr=${directErr}`)
    const mixed = await pipeline({ type: "file", filename: "quarterly-report-encrypted.docx", mime: mimes.docx, source: { path: encrypted } }, "encrypted")
    assert(mixed.includes("[media-preprocess failed: kind=document") && mixed.includes("[media-preprocess text-file:") === false, `encrypted document was not contained: ${mixed}`)
    const mixedParts = { parts: [{ type: "text", text: "Please inspect attachments." }, { type: "file", filename: "quarterly-report-encrypted.docx", mime: mimes.docx, source: { path: encrypted } }, attachment("csv", "quarterly-report.csv", mimes.csv, file("quarterly-report.csv"))] }
    const mixedPlugin = await MediaGuardPlugin({}, { materializationDir: join(root, "encrypted-mixed-stage"), cacheDir: join(root, "encrypted-mixed-cache"), timeoutMs: 300000 })
    await mixedPlugin["chat.message"]!({}, mixedParts)
    const mixedOutput = partsText(mixedParts)
    assert(mixedOutput.includes("[media-preprocess failed: kind=document") && mixedOutput.includes("[media-preprocess text-file:"), `mixed encrypted transform lost other part: ${mixedOutput}`)
    console.log("PASS encrypted docx: direct anydoc failure and contained mixed pipeline failure")
  }
  console.log(`PASS anydoc plugin E2E: ${formats.length} documents + ZIP + CSV + corrupt document`)
  }
} finally { rmSync(root, { recursive: true, force: true }) }
