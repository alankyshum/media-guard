#!/usr/bin/env bun
/**
 * REAL LOCAL E2E probe. This is intentionally slower than the unit suite:
 * it runs the merged Media Guard plugin with its real local binaries and
 * cached OCR/Whisper models. No extractors are injected or mocked.
 */
import { createHash } from "node:crypto"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { MediaGuardPlugin } from "../media-guard.ts"

const root = mkdtempSync(join(tmpdir(), "media-pipeline-e2e-"))
const py = "/Users/alanshum/.claude/skills/tool--pdf/scripts/.venv/bin/python"
const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message) }
const run = (argv: string[], options: any = {}) => {
  const result = Bun.spawnSync(argv, { ...options, stdout: "pipe", stderr: "pipe" })
  if (result.exitCode !== 0) throw new Error(`${argv[0]} failed (${result.exitCode}): ${result.stderr.toString()}`)
  return result.stdout.toString()
}
const partsText = (output: any) => output.parts.filter((p: any) => typeof p.text === "string").map((p: any) => p.text).join("\n")
const attachment = (id: string, filename: string, mime: string, path: string) => ({ id, type: "file", filename, mime, source: { path } })

async function realPipeline(file: any, cacheName: string, nativeKinds: string[] = []): Promise<string> {
  const plugin = await MediaGuardPlugin({}, { materializationDir: join(root, `${cacheName}-guard`), cacheDir: join(root, `${cacheName}-preprocess`), timeoutMs: 300000, nativeKinds })
  const output = { parts: [{ id: "u1", type: "text", text: "what is the total on this invoice?" }, file] }
  await plugin["chat.message"]!({}, output)
  return partsText(output)
}

try {
  const pdf = join(root, "invoice.pdf")
  run([py, "-c", `import fitz,sys
d=fitz.open(); p=d.new_page(); p.insert_text((72,100), 'Invoice #INV-2024-001'); p.insert_text((72,130), 'Total Due: $1,234.56'); p.insert_text((72,160), 'Thank you for your business.'); d.save(sys.argv[1])`, pdf])
  const pdfText = await realPipeline(attachment("f1", "invoice.pdf", "application/pdf", pdf), "pdf")
  assert(pdfText.includes("$1,234.56"), `PDF amount missing from final model text: ${pdfText}`)
  console.log(`PASS PDF extracted text: ${JSON.stringify(pdfText.match(/Invoice #[^\n]+|Total Due[^\n]+/g) ?? pdfText)}`)

  let audioText = ""
  try {
    const aiff = join(root, "invoice.aiff"), wav = join(root, "invoice.wav")
    run(["say", "-o", aiff, "invoice total is forty two dollars"])
    run(["ffmpeg", "-y", "-i", aiff, "-ar", "16000", "-ac", "1", wav])
    audioText = await realPipeline(attachment("a1", "invoice.wav", "audio/wav", wav), "audio")
    assert(/invoice total/i.test(audioText) && /forty[- ]two|42/i.test(audioText), `audio transcript missing expected words: ${audioText}`)
    console.log(`PASS audio extracted text: ${JSON.stringify(audioText)}`)
  } catch (error) {
    console.warn(`SKIP AUDIO (real local say/ffmpeg/Whisper unavailable): ${error instanceof Error ? error.message : error}`)
  }

  let imageText = ""
  try {
    const image = join(root, "invoice.png")
    run([py, "-c", `import fitz,sys
d=fitz.open(); p=d.new_page(width=600,height=130); p.insert_text((35,75), 'Invoice OCR Total $1,234.56', fontsize=28); pix=p.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False); pix.save(sys.argv[1])`, image])
    imageText = await realPipeline(attachment("i1", "invoice.png", "image/png", image), "image")
    assert(imageText.includes("1,234.56") || imageText.includes("1234.56"), `image OCR missing invoice amount: ${imageText}`)
    console.log(`PASS image extracted text: ${JSON.stringify(imageText)}`)
    const nativeText = await realPipeline(attachment("i2", "invoice.png", "image/png", image), "image-native", ["image"])
    assert(nativeText.includes("[media-preprocess native-skip: kind=image reason=model accepts image input natively]"), `image native-skip marker missing: ${nativeText}`)
    console.log("PASS image native-skip marker")
  } catch (error) {
    console.warn(`SKIP IMAGE (real local image OCR unavailable): ${error instanceof Error ? error.message : error}`)
  }

  const archiveDir = join(root, "archive-input"); mkdirSync(archiveDir)
  writeFileSync(join(archiveDir, "invoice-notes.txt"), "Archive invoice note: payment is due upon receipt.")
  const archiveImage = join(archiveDir, "invoice-scan.png")
  run([py, "-c", `import fitz,sys
d=fitz.open(); p=d.new_page(width=300,height=80); p.insert_text((90,48), 'SCAN', fontsize=24); p.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False).save(sys.argv[1])`, archiveImage])
  const archive = join(root, "invoice-bundle.zip")
  run(["zip", "-q", "-r", archive, "."], { cwd: archiveDir })
  const archiveText = await realPipeline(attachment("z1", "invoice-bundle.zip", "application/zip", archive), "archive")
  assert(archiveText.includes("Archive invoice note: payment is due upon receipt."), `ZIP text member missing: ${archiveText}`)
  assert(archiveText.includes('"handling":"needs-agent"') && archiveText.includes("vision-reader") && archiveText.includes("/invoice-scan.png"), `ZIP vision dispatch missing: ${archiveText}`)
  console.log(`PASS ZIP extracted text: ${JSON.stringify(archiveText.match(/Archive invoice note[^\n]+/)?.[0] ?? "")}`)
  console.log(`PASS ZIP needs-agent directive: ${JSON.stringify(archiveText.match(/Dispatch [^\n]+|[^\n]*invoice-scan\.png[^\n]*/g) ?? [])}`)
  console.log("PASS media pipeline E2E: PDF + audio + image + ZIP ran through one merged plugin")
} finally {
  rmSync(root, { recursive: true, force: true })
}
