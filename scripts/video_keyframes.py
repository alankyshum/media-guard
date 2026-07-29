#!/usr/bin/env python3
"""Extract scene-guided WebP keyframes from a video using ffmpeg and Pillow.

Provenance: written independently from public ffmpeg documentation and first
principles. Does not derive from, copy, or port any AGPL-licensed project.
Algorithm: ffmpeg `select='gt(scene,T)',metadata=print` scene detection
parsing `pts_time`, scene-guided timestamp selection with midpoint insertion
for long segments, `-ss` extraction, Pillow WebP conversion.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image


PTS_TIME = re.compile(r"pts_time:\s*([-+]?\d+(?:\.\d+)?)")


def executable(name: str, env_name: str) -> str:
    override = os.environ.get(env_name)
    value = override or shutil.which(name)
    if not value:
        raise RuntimeError(
            f"{name} is required for video keyframes. Install ffmpeg and ensure {name} is on PATH, "
            f"or set {env_name}."
        )
    return value


def run(argv: list[str], *, capture_output: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE if capture_output else None, stderr=subprocess.PIPE)


def duration_seconds(ffprobe: str, video: str) -> float:
    result = run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video])
    try:
        duration = float(result.stdout.decode().strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe did not return a usable video duration") from exc
    if duration <= 0:
        raise RuntimeError("video duration must be positive")
    return duration


def scene_changes(ffmpeg: str, video: str, threshold: float) -> list[float]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", video, "-filter:v", f"select='gt(scene,{threshold})',metadata=print", "-an", "-f", "null", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg scene detection failed with status {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
    return sorted({float(match.group(1)) for match in PTS_TIME.finditer(output.decode(errors="replace")) if float(match.group(1)) >= 0})


def timestamps_for_video(ffmpeg: str, ffprobe: str, video: str, threshold: float, max_frames: int) -> tuple[list[float], int]:
    duration = duration_seconds(ffprobe, video)
    changes = scene_changes(ffmpeg, video, threshold)
    boundaries = [0.0] + [timestamp for timestamp in changes if timestamp < duration]
    boundaries = sorted(set(boundaries))
    timestamps = list(boundaries)
    for start, end in zip(boundaries, boundaries[1:] + [duration]):
        if end - start > 3.0:
            timestamps.append(start + (end - start) / 2)
    timestamps = sorted({round(timestamp, 6) for timestamp in timestamps if 0 <= timestamp < duration})
    if not changes:
        count = min(max_frames, max(1, 4 if duration > 3 else 2))
        timestamps = [0.0] if count == 1 else [round((duration - min(0.1, duration / 10)) * i / (count - 1), 6) for i in range(count)]
    total = len(timestamps)
    return timestamps[:max_frames], total


def extract_frame(ffmpeg: str, video: str, timestamp: float, long_edge: int) -> Image.Image:
    result = run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.6f}", "-i", video, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"])
    image = Image.open(BytesIO(result.stdout)).convert("RGB")
    scale = long_edge / max(image.size)
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_path")
    parser.add_argument("output_dir")
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--long-edge", type=int, default=1568)
    args = parser.parse_args()
    if args.max_frames < 1 or args.threshold < 0 or args.long_edge < 1:
        parser.error("--max-frames, --threshold, and --long-edge must be positive (threshold may be zero)")

    ffmpeg = executable("ffmpeg", "MEDIA_GUARD_FFMPEG")
    ffprobe = executable("ffprobe", "MEDIA_GUARD_FFPROBE")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamps, total = timestamps_for_video(ffmpeg, ffprobe, args.video_path, args.threshold, args.max_frames)
    for index, timestamp in enumerate(timestamps, 1):
        output = output_dir / f"frame-{index:04d}.webp"
        extract_frame(ffmpeg, args.video_path, timestamp, args.long_edge).save(output, "WEBP", quality=75)
    complete_path = output_dir / ".complete"
    complete_path.write_text(f"{len(timestamps)}\n{total}\n")
    os.chmod(complete_path, 0o600)
    print(json.dumps({"frames": [{"path": str(output_dir / f"frame-{index:04d}.webp"), "timestamp_seconds": timestamp} for index, timestamp in enumerate(timestamps, 1)]}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"video keyframe extraction failed: {exc}", file=sys.stderr)
        sys.exit(1)
