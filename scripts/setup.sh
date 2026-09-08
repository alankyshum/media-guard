#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
venv="${MEDIA_GUARD_VENV:-$root/.venv}"
python="${MEDIA_GUARD_BOOTSTRAP_PYTHON:-$(command -v python3 || true)}"
uv_cache_dir="${UV_CACHE_DIR:-$HOME/.cache/uv}"
uv_link_mode="${UV_LINK_MODE:-hardlink}"

if command -v uv >/dev/null 2>&1; then
  uv_bin="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
  uv_bin="$HOME/.local/bin/uv"
else
  printf '%s\n' 'media-guard setup FAILED: uv was not found on PATH or at ~/.local/bin/uv. Install uv or make it available to launchd.' >&2
  exit 1
fi
if [ -z "$python" ]; then
  printf '%s\n' 'media-guard setup FAILED: python3 is required to create the virtual environment.' >&2
  exit 1
fi
case "$venv" in /*) ;; *) printf 'media-guard setup FAILED: MEDIA_GUARD_VENV must be an absolute path: %s\n' "$venv" >&2; exit 1 ;; esac
case "$uv_link_mode" in clone|copy|hardlink|symlink) ;; *) printf 'media-guard setup FAILED: unsupported UV_LINK_MODE: %s\n' "$uv_link_mode" >&2; exit 1 ;; esac
if [ -e "$venv" ] && [ ! -f "$venv/pyvenv.cfg" ]; then
  printf 'media-guard setup FAILED: refusing to replace non-venv path: %s. Remove or repair it explicitly.\n' "$venv" >&2
  exit 1
fi
requested_version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || {
  printf 'media-guard setup FAILED: MEDIA_GUARD_BOOTSTRAP_PYTHON is not executable or cannot report its Python version: %s\n' "$python" >&2
  exit 1
}
mkdir -p "$uv_cache_dir"
cache_df="$(df -P "$uv_cache_dir")" || { printf 'media-guard setup FAILED: could not determine filesystem for UV_CACHE_DIR: %s\n' "$uv_cache_dir" >&2; exit 1; }
cache_fs="$(printf '%s\n' "$cache_df" | awk 'NR == 2 { print $1 }')"
venv_parent="$(dirname "$venv")"
mkdir -p "$venv_parent"
venv_df="$(df -P "$venv_parent")" || { printf 'media-guard setup FAILED: could not determine filesystem for venv parent: %s\n' "$venv_parent" >&2; exit 1; }
venv_fs="$(printf '%s\n' "$venv_df" | awk 'NR == 2 { print $1 }')"
if [ -z "$cache_fs" ] || [ -z "$venv_fs" ]; then
  printf '%s\n' 'media-guard setup FAILED: df returned no filesystem identifier; cannot safely select uv link mode.' >&2
  exit 1
fi
selected_link_mode="$uv_link_mode"
if { [ "$uv_link_mode" = hardlink ] || [ "$uv_link_mode" = clone ]; } && [ "$cache_fs" != "$venv_fs" ]; then
  selected_link_mode=copy
  printf 'media-guard setup WARNING: cache and venv are on different filesystems; requested %s links cannot be used; selected copy.\n' "$uv_link_mode" >&2
fi

rebuild_reason=''
if [ -e "$venv" ]; then
  if [ ! -x "$venv/bin/python" ]; then
    rebuild_reason='missing venv Python'
  elif ! current_version="$("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
    rebuild_reason='broken venv Python'
  elif [ "$current_version" != "$requested_version" ]; then
    rebuild_reason="Python $current_version does not match bootstrap Python $requested_version"
  fi
fi
backup_dir=''
backup_venv=''
rebuild_active=0
backup_moved=0
had_venv=0
rollback_rebuild() {
  status=$1
  trap - EXIT HUP INT TERM
  [ "$rebuild_active" = 1 ] || exit "$status"
  rebuild_active=0
  if [ "$backup_moved" = 1 ] && [ -e "$backup_venv" ]; then
    rm -rf "$venv" || { printf 'media-guard setup FAILED: rebuild cleanup failed; rollback backup remains at %s\n' "$backup_dir" >&2; exit "$status"; }
    [ ! -e "$venv" ] || { printf 'media-guard setup FAILED: rebuild cleanup left venv in place; rollback backup remains at %s\n' "$backup_dir" >&2; exit "$status"; }
    mv "$backup_venv" "$venv" || { printf 'media-guard setup FAILED: rebuild failed and rollback backup remains at %s\n' "$backup_dir" >&2; exit "$status"; }
    rmdir "$backup_dir" || printf 'media-guard setup FAILED: rebuilt rollback container remains at %s\n' "$backup_dir" >&2
  elif [ -n "$backup_dir" ]; then
    rmdir "$backup_dir" || printf 'media-guard setup FAILED: unused rollback container remains at %s\n' "$backup_dir" >&2
  elif [ "$had_venv" = 0 ]; then
    rm -rf "$venv" || printf 'media-guard setup FAILED: rebuild cleanup failed at %s\n' "$venv" >&2
  fi
  exit "$status"
}
if [ ! -e "$venv" ] || [ -n "$rebuild_reason" ]; then
  [ -z "$rebuild_reason" ] || printf 'media-guard setup: rebuilding %s (%s).\n' "$venv" "$rebuild_reason" >&2
  # Venv entry points embed their creation path. Preserve the old venv as a
  # rollback backup and recreate at the final path rather than renaming a
  # sibling venv whose entry-point shebangs would be wrong after a swap.
  [ ! -e "$venv" ] || had_venv=1
  rebuild_active=1
  trap 'rollback_rebuild $?' EXIT
  trap 'rollback_rebuild 129' HUP
  trap 'rollback_rebuild 130' INT
  trap 'rollback_rebuild 143' TERM
  if [ -e "$venv" ]; then
    backup_dir="$(mktemp -d "${venv}.rebuild-backup.XXXXXX")" || { printf 'media-guard setup FAILED: could not reserve a rollback backup path beside %s\n' "$venv" >&2; exit 1; }
    backup_venv="$backup_dir/venv"
    backup_moved=1
    mv "$venv" "$backup_venv"
  fi
  "$uv_bin" venv --clear --cache-dir "$uv_cache_dir" --link-mode "$selected_link_mode" --python "$python" "$venv"
fi
"$uv_bin" pip install --cache-dir "$uv_cache_dir" --link-mode "$selected_link_mode" --python "$venv/bin/python" -r "$root/scripts/requirements.txt"

case "$(uname -s):$(uname -m)" in
  Darwin:arm64|Darwin:aarch64)
    "$uv_bin" pip install --cache-dir "$uv_cache_dir" --link-mode "$selected_link_mode" --python "$venv/bin/python" mlx-whisper
    backend='mlx-whisper'
    ;;
  *)
    "$uv_bin" pip install --cache-dir "$uv_cache_dir" --link-mode "$selected_link_mode" --python "$venv/bin/python" openai-whisper
    backend='openai-whisper'
    ;;
esac

"$venv/bin/python" -c 'import fitz, PIL'
rebuild_active=0
trap - EXIT HUP INT TERM
if [ -n "$backup_dir" ]; then
  rm -rf "$backup_dir" || {
    printf 'media-guard setup FAILED: validated venv retained, but rollback backup cleanup failed: %s. Remove it manually.\n' "$backup_dir" >&2
    exit 1
  }
fi

printf 'media-guard setup READY: %s (backend: %s, cache: %s, requested link mode: %s, selected link mode: %s)\n' "$venv" "$backend" "$uv_cache_dir" "$uv_link_mode" "$selected_link_mode"
