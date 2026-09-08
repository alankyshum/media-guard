#!/usr/bin/env bun
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { spawn, spawnSync } from "node:child_process"

const root = join(import.meta.dir, "..")
const setup = join(root, "scripts", "setup.sh")
const dir = mkdtempSync(join(tmpdir(), "media-guard-setup-"))
const makeExecutable = (path, text) => { writeFileSync(path, text); chmodSync(path, 0o755) }
const run = (env) => spawnSync("sh", [setup], { env: { HOME: env.HOME, PATH: env.PATH, UV_LOG: env.UV_LOG, MEDIA_GUARD_BOOTSTRAP_PYTHON: env.MEDIA_GUARD_BOOTSTRAP_PYTHON, UV_CACHE_DIR: env.UV_CACHE_DIR, UV_LINK_MODE: "hardlink", ...env }, encoding: "utf8" })
const backupPaths = (venv) => readdirSync(join(venv, ".."), { withFileTypes: true }).filter(entry => entry.name.startsWith(`${venv.split("/").pop()}.rebuild-backup.`))
const waitFor = async (path, description) => {
  const deadline = Date.now() + 5000
  while (!existsSync(path) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10))
  if (!existsSync(path)) throw new Error(`timed out waiting for ${description}`)
}
try {
  const bin = join(dir, "bin"), home = join(dir, "home"), pythonDir = join(dir, "python space")
  mkdirSync(bin); mkdirSync(join(home, ".local", "bin"), { recursive: true }); mkdirSync(pythonDir)
  const python = join(pythonDir, "python")
  makeExecutable(python, "#!/bin/sh\nprintf '3.14\\n'\n")
  const log = join(dir, "uv.log")
  const uv = join(bin, "uv")
  makeExecutable(join(bin, "df"), `#!/bin/sh
if [ "\${TEST_CROSS_FS:-}" = 1 ]; then case "$2" in *cache*) device=cache-device ;; *) device=venv-device ;; esac; else device=shared-device; fi
printf 'Filesystem 512-blocks Used Available Capacity Mounted on\\n%s 1 1 1 1%% /\\n' "$device"
`)
  makeExecutable(join(bin, "mv"), `#!/bin/sh
set -eu
if [ -n "\${MOVE_LOG:-}" ]; then printf '%s\\n' "$*" >> "$MOVE_LOG"; fi
if [ -n "\${MOVE_WINDOW_READY:-}" ] && [ ! -e "$MOVE_WINDOW_READY" ]; then
  /bin/mv "$@"
  : > "$MOVE_WINDOW_READY"
  while [ ! -e "$MOVE_WINDOW_RELEASE" ]; do sleep 0.01; done
  exit 0
fi
exec /bin/mv "$@"
`)
  makeExecutable(join(bin, "rm"), `#!/bin/sh
set -eu
for arg in "$@"; do
  case "$arg" in
    *.rebuild-backup.*) [ "\${TEST_FAIL_BACKUP_CLEANUP:-}" != 1 ] || exit 1 ;;
  esac
done
exec /bin/rm "$@"
`)
  makeExecutable(uv, `#!/bin/sh
printf '%s\\n' "$*" >> "$UV_LOG"
if [ "$1 $2" = 'venv --clear' ]; then if [ "\${UV_BLOCK_VENV:-}" = 1 ]; then : > "$UV_BLOCK_READY"; while [ ! -e "$UV_BLOCK_RELEASE" ]; do sleep 0.01; done; fi; target=''; requested=''; previous=''; for arg in "$@"; do target=$arg; [ "$previous" = --python ] && requested=$arg; previous=$arg; done; mkdir -p "$target/bin"; : > "$target/pyvenv.cfg"; ln -sf "$requested" "$target/bin/python"; fi
[ "$1" != pip ] || [ "\${UV_FAIL_PIP:-}" != 1 ] || exit 1
`)
   const base = { HOME: home, PATH: `${bin}:/usr/bin:/bin`, UV_LOG: log, MEDIA_GUARD_BOOTSTRAP_PYTHON: python, UV_CACHE_DIR: join(dir, "cache") }
  const venv = join(dir, "healthy venv"); mkdirSync(join(venv, "bin"), { recursive: true }); writeFileSync(join(venv, "pyvenv.cfg"), ""); symlinkSync(python, join(venv, "bin", "python"))
  let result = run({ ...base, MEDIA_GUARD_VENV: venv })
  if (result.status !== 0 || readFileSync(log, "utf8").includes("venv --clear")) throw new Error(`healthy venv was not retained: ${result.stderr}`)
  const ambientCopy = join(pythonDir, "ambient copied python"); copyFileSync(python, ambientCopy); chmodSync(ambientCopy, 0o755)
  const ambientCopyVenv = join(dir, "ambient copy venv")
  result = run({ ...base, MEDIA_GUARD_BOOTSTRAP_PYTHON: ambientCopy, MEDIA_GUARD_VENV: ambientCopyVenv })
  if (result.status !== 0 || !existsSync(join(ambientCopyVenv, "pyvenv.cfg"))) throw new Error(`ambient copied Python did not create a venv: ${result.stderr}`)
  const ambientLink = join(pythonDir, "ambient linked python"); symlinkSync(python, ambientLink)
  const ambientLinkVenv = join(dir, "ambient link venv")
  result = run({ ...base, MEDIA_GUARD_BOOTSTRAP_PYTHON: ambientLink, MEDIA_GUARD_VENV: ambientLinkVenv })
  if (result.status !== 0 || !existsSync(join(ambientLinkVenv, "pyvenv.cfg"))) throw new Error(`ambient linked Python did not create a venv: ${result.stderr}`)
  rmSync(join(venv, "bin", "python"))
  result = run({ ...base, MEDIA_GUARD_VENV: venv })
  if (result.status !== 0 || !readFileSync(log, "utf8").includes(`--python ${python} ${venv}`)) throw new Error(`broken venv was not rebuilt with resolved Python: ${result.stderr}`)
  const wrongPython = join(pythonDir, "wrong-python"); makeExecutable(wrongPython, "#!/bin/sh\nprintf '3.13\\n'\n"); rmSync(join(venv, "bin", "python")); symlinkSync(wrongPython, join(venv, "bin", "python"))
   result = run({ ...base, MEDIA_GUARD_VENV: venv })
   if (result.status !== 0 || !result.stderr.includes("Python 3.13 does not match bootstrap Python 3.14")) throw new Error(`wrong-Python venv was not rebuilt: ${result.stderr}`)
   rmSync(join(venv, "bin", "python")); symlinkSync(wrongPython, join(venv, "bin", "python"))
   result = run({ ...base, MEDIA_GUARD_VENV: venv, UV_FAIL_PIP: "1" })
   if (result.status === 0 || readlinkSync(join(venv, "bin", "python")) !== wrongPython || backupPaths(venv).length) throw new Error(`failed rebuild did not restore the previous venv: ${result.stderr}`)
   for (const [name, number, status] of [["TERM", 15, 143], ["HUP", 1, 129], ["INT", 2, 130]]) {
     rmSync(join(venv, "bin", "python")); symlinkSync(wrongPython, join(venv, "bin", "python"))
     writeFileSync(join(venv, "original-marker"), `original marker ${name}\n`)
     const ready = join(dir, `${name}.ready`), release = join(dir, `${name}.release`), moveLog = join(dir, `${name}.moves`)
     const child = spawn("sh", [setup], { env: { ...base, MEDIA_GUARD_VENV: venv, MOVE_LOG: moveLog, MOVE_WINDOW_READY: ready, MOVE_WINDOW_RELEASE: release }, stdio: "pipe" })
     await waitFor(ready, `${name} post-move window`)
     child.kill(`SIG${name}`)
     writeFileSync(release, "")
     const signalResult = await new Promise((resolve, reject) => {
       const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error(`timed out waiting for ${name} setup`)) }, 5000)
       child.on("close", (code, signal) => { clearTimeout(timer); resolve({ code, signal }) })
     })
     if (signalResult.code !== status || signalResult.signal !== null || readlinkSync(join(venv, "bin", "python")) !== wrongPython || readFileSync(join(venv, "original-marker"), "utf8") !== `original marker ${name}\n` || !existsSync(join(venv, "pyvenv.cfg")) || backupPaths(venv).length || readFileSync(moveLog, "utf8").trim().split("\n").length !== 2) throw new Error(`${name} move-window rollback did not restore the original venv exactly once: ${JSON.stringify(signalResult)}`)
   }
   const staleBackup = `${venv}.rebuild-backup.stale`; mkdirSync(staleBackup); writeFileSync(join(staleBackup, "marker"), "preserve me\n")
   rmSync(join(venv, "bin", "python")); symlinkSync(wrongPython, join(venv, "bin", "python"))
   result = run({ ...base, MEDIA_GUARD_VENV: venv })
   if (result.status !== 0 || readFileSync(join(staleBackup, "marker"), "utf8") !== "preserve me\n" || !readFileSync(log, "utf8").includes("venv --clear") || !readFileSync(log, "utf8").includes("pip install") || !existsSync(join(venv, "pyvenv.cfg")) || backupPaths(venv).length !== 1) throw new Error(`stale backup did not prove a distinct completed rebuild: ${result.stderr}`)
   rmSync(join(venv, "bin", "python")); symlinkSync(wrongPython, join(venv, "bin", "python"))
   result = run({ ...base, MEDIA_GUARD_VENV: venv, TEST_FAIL_BACKUP_CLEANUP: "1" })
   if (result.status === 0 || !result.stderr.includes("validated venv retained, but rollback backup cleanup failed") || readlinkSync(join(venv, "bin", "python")) !== python || !existsSync(join(venv, "pyvenv.cfg")) || backupPaths(venv).length !== 2) throw new Error(`cleanup failure rearmed rollback or was not actionable: ${result.stderr}`)
   for (const entry of backupPaths(venv)) if (entry.name !== staleBackup.split("/").pop()) rmSync(join(venv, "..", entry.name), { recursive: true })
  const rejected = join(dir, "not a venv"); mkdirSync(rejected)
  result = run({ ...base, MEDIA_GUARD_VENV: rejected, UV_CACHE_DIR: join(dir, "rejected-cache") })
  if (result.status === 0 || !result.stderr.includes("refusing to replace non-venv path") || existsSync(join(dir, "rejected-cache"))) throw new Error(`non-venv refusal was unsafe: ${result.stderr}`)
  result = run({ ...base, TEST_CROSS_FS: "1", MEDIA_GUARD_VENV: join(dir, "copy venv"), UV_LINK_MODE: "hardlink", UV_CACHE_DIR: join(dir, "cross-cache") })
  if (result.status !== 0 || !result.stdout.includes("selected link mode: copy")) throw new Error(`cross-filesystem copy selection failed: ${result.stderr}`)
   result = run({ ...base, MEDIA_GUARD_BOOTSTRAP_PYTHON: join(dir, "missing-python"), MEDIA_GUARD_VENV: join(dir, "invalid-python venv") })
   if (result.status === 0 || !result.stderr.includes("MEDIA_GUARD_BOOTSTRAP_PYTHON is not executable or cannot report its Python version")) throw new Error(`bootstrap Python error is not actionable: ${result.stderr}`)
   result = run({ ...base, MEDIA_GUARD_VENV: join(dir, "invalid-link venv"), UV_LINK_MODE: "invalid" })
   if (result.status === 0 || !result.stderr.includes("unsupported UV_LINK_MODE: invalid")) throw new Error(`invalid link mode was accepted: ${result.stderr}`)
   result = run({ ...base, MEDIA_GUARD_VENV: "relative-venv" })
   if (result.status === 0 || !result.stderr.includes("MEDIA_GUARD_VENV must be an absolute path: relative-venv")) throw new Error(`relative venv path was accepted: ${result.stderr}`)
   rmSync(uv)
   result = run({ ...base, MEDIA_GUARD_VENV: join(dir, "missing-uv venv") })
   if (result.status === 0 || !result.stderr.includes("uv was not found on PATH")) throw new Error(`missing uv was not actionable: ${result.stderr}`)
  // The fallback path is separately checked with a minimal shim to avoid using PATH's uv.
  makeExecutable(join(home, ".local", "bin", "uv"), `#!/bin/sh
if [ "$1 $2" = 'venv --clear' ]; then target=''; for arg in "$@"; do target=$arg; done; mkdir -p "$target/bin"; : > "$target/pyvenv.cfg"; ln -sf "$MEDIA_GUARD_BOOTSTRAP_PYTHON" "$target/bin/python"; fi
`)
  result = run({ ...base, PATH: "/usr/bin:/bin", MEDIA_GUARD_VENV: join(dir, "fallback venv"), UV_CACHE_DIR: join(dir, "fallback-cache") })
  if (result.status !== 0 || !existsSync(join(dir, "fallback venv", "pyvenv.cfg"))) throw new Error(`uv fallback did not create a venv: ${result.stderr}`)
  console.log("media-guard setup regression: PASS")
} finally { rmSync(dir, { recursive: true, force: true }) }
