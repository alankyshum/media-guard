#!/usr/bin/env bun
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { pathToFileURL } from "node:url"

const source = join(import.meta.dir, "..", "media-guard.ts")

async function fixture(config) {
  const root = mkdtempSync(join(tmpdir(), "media-guard-config-"))
  const pluginDir = join(root, "external", "media-guard")
  mkdirSync(pluginDir, { recursive: true })
  mkdirSync(join(root, "config", "agent-runtime"), { recursive: true })
  copyFileSync(source, join(pluginDir, "media-guard.ts"))
  writeFileSync(join(root, "config", "agent-runtime", "agent-config.yml"), config)
  const url = `${pathToFileURL(join(pluginDir, "media-guard.ts"))}?fixture=${Date.now()}-${Math.random()}`
  const module = await import(url)
  return { root, create: module.default }
}

{
  const { root, create } = await fixture("harnesses: [opencode, omp]\n")
  try {
    await create({ directory: root, worktree: root })
    throw new Error("missing plugins.media_guard did not fail")
  } catch (error) {
    if (!String(error).includes("plugins:")) throw error
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

{
  const { root, create } = await fixture(`plugins:
  media_guard:
    maxMaterializedFilesPerTransform: 1
`)
  try {
    const hooks = await create({ directory: root, worktree: root })
    const parts = [
      { type: "file", mime: "image/png", filename: "one.png", url: "data:image/png;base64,AA==" },
      { type: "file", mime: "image/png", filename: "two.png", url: "data:image/png;base64,AA==" },
    ]
    await hooks["chat.message"]({}, { parts })
    for (const part of parts) {
      if (!part.text?.includes("maxFilesPerTransform (1)")) {
        throw new Error(`nested maxMaterializedFilesPerTransform was not applied: ${JSON.stringify(part)}`)
      }
    }
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

console.log("PASS plugins.media_guard loads and missing block fails loudly")
