#!/usr/bin/env bun
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { spawnSync } from "node:child_process"

const testDir = import.meta.dir
const dotfiles = process.env.DOTFILES_DIR || join(process.env.DOCUMENTS_PATH || "/Volumes/Documents", "dotfiles")
const configPath = process.env.MEDIA_GUARD_CONFIG || join(dotfiles, "config", "opencode", "opencode.jsonc")
const config = readFileSync(configPath, "utf8")
const wrapper = readFileSync(join(dotfiles, "config", "opencode", "plugins", "media-guard", "index.ts"), "utf8")
const implementation = readFileSync(join(testDir, "..", "media-guard.ts"), "utf8")

if (!config.includes('"./plugins/media-guard"')) throw new Error("wrapper plugin is not registered")
if (!wrapper.includes('from "../../../../external/media-guard/media-guard.ts"')) throw new Error("wrapper does not target media-guard")
for (const required of ["data:", "source", "filename", "size", "media_kind", "[media-guard attachment manifest]", "type: \"text\""]) {
  if (!implementation.includes(required)) throw new Error(`manifest behavior missing: ${required}`)
}
const result = spawnSync("bun", [join(testDir, "verify_120.mjs")], { encoding: "utf8" })
if (result.status !== 0) throw new Error(`manifest regression failed:\n${result.stdout}\n${result.stderr}`)
console.log("PASS media-guard configuration and manifest behavior")
