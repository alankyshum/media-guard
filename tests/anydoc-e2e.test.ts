#!/usr/bin/env bun
/** Real anydoc integration probe: documents are generated locally, then materialized and preprocessed. */
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { MediaGuardPlugin } from "../media-guard.ts"

const root = mkdtempSync(join(tmpdir(), "anydoc-plugin-e2e-"))
const fixtures = process.env.ANYDOC_REAL_FIXTURES ?? "/tmp/anydoc-real"
const assert = (value: unknown, message: string) => { if (!value) throw new Error(message) }
const partsText = (output: any) => output.parts.filter((p: any) => typeof p.text === "string").map((p: any) => p.text).join("\n")
const attachment = (id: string, filename: string, mime: string, path: string) => ({ id, type: "file", filename, mime, source: { path } })
const file = (name: string) => join(fixtures, name)
const mimes: Record<string, string> = { docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation", odt: "application/vnd.oasis.opendocument.text", ods: "application/vnd.oasis.opendocument.spreadsheet", odp: "application/vnd.oasis.opendocument.presentation", rtf: "application/rtf", epub: "application/epub+zip", csv: "text/csv" }
async function pipeline(part: any, name: string) {
  const plugin = await MediaGuardPlugin({}, { materializationDir: join(root, `${name}-stage`), cacheDir: join(root, `${name}-cache`), timeoutMs: 300000 })
  const output = { parts: [{ type: "text", text: "Please inspect attachment." }, part] }
  await plugin["chat.message"]!({}, output)
  return partsText(output)
}
try {
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
  console.log(`PASS anydoc plugin E2E: ${formats.length} documents + ZIP + CSV + corrupt document`)
} finally { rmSync(root, { recursive: true, force: true }) }
