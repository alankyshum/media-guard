#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
venv="${MEDIA_GUARD_VENV:-$root/.venv}"
python="${MEDIA_GUARD_BOOTSTRAP_PYTHON:-$(command -v python3 || true)}"

if [ -z "$python" ]; then
  printf '%s\n' 'media-guard setup FAILED: python3 is required to create the virtual environment.' >&2
  exit 1
fi

"$python" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -r "$root/scripts/requirements.txt"

case "$(uname -s):$(uname -m)" in
  Darwin:arm64|Darwin:aarch64)
    "$venv/bin/python" -m pip install mlx-whisper
    backend='mlx-whisper'
    ;;
  *)
    "$venv/bin/python" -m pip install openai-whisper
    backend='openai-whisper'
    ;;
esac

printf 'media-guard setup READY: %s (backend: %s)\n' "$venv" "$backend"
