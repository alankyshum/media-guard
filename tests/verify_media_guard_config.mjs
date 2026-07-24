#!/usr/bin/env bun
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const configPath = process.env.MEDIA_GUARD_CONFIG || join(testDir, "..", "..", "..", "config", "opencode", "opencode.jsonc")
const config = readFileSync(configPath, "utf8")
const plugin = readFileSync(join(testDir, "..", "media-guard.ts"), "utf8")

if (!config.includes('"../../external/media-guard/media-guard.ts"')) throw new Error("manifest plugin not configured")
for (const required of ["data:", "source", "filename", "size", "media_kind", "synthetic: true"]) {
  if (!plugin.includes(required)) throw new Error(`manifest behavior missing: ${required}`)
}
for (const forbidden of ["api/chat", "fetch(", "spawn(", "exec("]) {
  if (plugin.toLowerCase().includes(forbidden.toLowerCase())) throw new Error(`plugin must not contain content-processing path: ${forbidden}`)
}
const result = spawnSync("bun", [join(testDir, "verify_120.mjs")], { encoding: "utf8" })
if (result.status !== 0) throw new Error(`manifest regression failed:\n${result.stdout}\n${result.stderr}`)
console.log("PASS media-guard configuration and manifest behavior")
