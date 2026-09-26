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
  return { root, plugin: module.default }
}

async function setup(plugin, root, options = {}) {
  let context
  const dispose = await plugin.setup({
    options,
    location: { directory: root },
    session: {
      hook: async (name, callback) => {
        if (name === "context") context = callback
        return { dispose: async () => {} }
      },
      get: async () => ({ location: { directory: root } }),
    },
  })
  return { context, dispose }
}

{
  const { root, plugin } = await fixture("harnesses: [opencode, omp]\n")
  try {
    await setup(plugin, root)
    throw new Error("missing plugins.media_guard did not fail")
  } catch (error) {
    if (!String(error).includes("plugins:")) throw error
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

{
  const { root, plugin } = await fixture(`plugins:
  media_guard:
    maxMaterializedFilesPerTransform: 1
`)
  try {
    const hooks = await setup(plugin, root)
    const parts = [
      { type: "file", mime: "image/png", filename: "one.png", url: "data:image/png;base64,AA==" },
      { type: "file", mime: "image/png", filename: "two.png", url: "data:image/png;base64,AA==" },
    ]
    await hooks.context({ sessionID: "config-test", messages: [{ parts }] })
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
