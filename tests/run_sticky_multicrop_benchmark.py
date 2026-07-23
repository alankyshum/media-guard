#!/usr/bin/env python3
"""Run exactly one hash-bound local sticky-note multicrop benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from score_sticky_notes import score


EXPECTED_ROUTE = "multicrop"
EXPECTED_TOOL = "qwen2.5vl-resident-multicrop"
EXPECTED_MODEL = "qwen2.5vl:32b"
EXPECTED_BASE_URL = "http://127.0.0.1:11434"
# Prefixes intentionally short: private benchmark artifacts stay outside repo.
AUTHORITATIVE_FIXTURE_SHA256_PREFIX = "8c9a81"
AUTHORITATIVE_ORACLE_SHA256_PREFIX = "03e01"
MAX_RSS_MB = 40 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_env(env: dict[str, str]) -> dict[str, str]:
    return {key: env[key] for key in sorted(env)}


def build_command(extract: Path, fixture: Path, timeout: float) -> list[str]:
    return [
        sys.executable,
        str(extract),
        str(fixture),
        "--kind",
        "image",
        "--ocr-engine",
        "sticky-notes",
        "--model",
        EXPECTED_MODEL,
        "--timeout",
        str(timeout),
        "--no-cache",
    ]


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "OCR_STICKY_MULTICROP_ENABLED": "1",
            "OCR_STICKY_RESIDENT_ENABLED": "1",
            "OCR_VLM_ENABLED": "1",
            "OCR_VLM_BASE_URL": EXPECTED_BASE_URL,
            "OCR_VLM_MODEL": EXPECTED_MODEL,
            "OCR_STICKY_MULTICROP_MAX_REQUESTS": "8",
        }
    )
    return env


def build_audit_command(extract: Path, fixture: Path, timeout: float) -> list[str]:
    return [sys.executable, str(extract), str(fixture), "--kind", "image", "--audit-only", "--timeout", str(timeout)]


def audit_current_source(extract: Path, fixture: Path, timeout: float) -> dict:
    completed = subprocess.run(
        build_audit_command(extract, fixture, timeout),
        env={**os.environ, "OCR_VLM_ENABLED": "0", "GRANITE_DOCLING_ENABLED": "0"},
        capture_output=True,
        text=True,
        timeout=max(timeout, 1.0) + 5,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("audit preflight failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("audit preflight output is not JSON") from error
    audit = (result.get("provenance") or {}).get("audit")
    if not isinstance(audit, dict):
        raise ValueError("audit preflight missing audit record")
    return audit


def extract_detections(result: dict) -> tuple[list[dict], dict]:
    payload = json.loads(result.get("text", ""))
    detections = payload.get("notes")
    if not isinstance(detections, list):
        raise ValueError("structured OCR text has no notes array")
    return detections, result.get("meta") if isinstance(result.get("meta"), dict) else {}


def rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return value / (1024 * 1024) if platform.system() == "Darwin" else value / 1024


def rejection(reason: str, **details: object) -> dict:
    return {"status": "rejected", "rejection_code": reason, **details}


def run_once(args: argparse.Namespace) -> tuple[int, dict]:
    fixture = args.fixture.resolve()
    oracle = args.oracle.resolve()
    extract = args.extract.resolve()
    out = args.out.resolve()
    if out.exists():
        raise ValueError(f"output already exists: {out}")
    if not fixture.is_file() or not oracle.is_file() or not extract.is_file():
        raise ValueError("fixture, oracle, and extractor must be files")
    if fixture.suffix.lower() not in {".heic", ".heif"}:
        raise ValueError("fixture must be the authoritative HEIC benchmark input")

    hashes = {
        "fixture_sha256": sha256_file(fixture),
        "oracle_sha256": sha256_file(oracle),
        "extractor_sha256": sha256_file(extract),
    }
    if not hashes["fixture_sha256"].startswith(AUTHORITATIVE_FIXTURE_SHA256_PREFIX):
        raise ValueError("fixture SHA-256 is not the authoritative HEIC benchmark input")
    if not hashes["oracle_sha256"].startswith(AUTHORITATIVE_ORACLE_SHA256_PREFIX):
        raise ValueError("oracle SHA-256 is not the authoritative benchmark oracle")

    dimensions = image_dimensions(fixture)
    if dimensions is None:
        raise ValueError("authoritative fixture dimensions unavailable")

    audit = audit_current_source(extract, fixture, args.timeout)
    if audit.get("input_sha256") != hashes["fixture_sha256"] or audit.get("extractor_source_sha256") != hashes["extractor_sha256"]:
        raise ValueError("audit preflight source/artifact hash mismatch")
    if audit.get("input_dimensions") != dimensions:
        raise ValueError("audit preflight dimensions mismatch")

    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex}"
    out.mkdir(parents=True)
    command = build_command(extract, fixture, args.timeout)
    env = build_env()
    started = time.monotonic()
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=args.timeout + 30)
    latency_ms = round((time.monotonic() - started) * 1000, 3)
    (out / "stdout.json").write_text(completed.stdout)
    (out / "stderr.txt").write_text(completed.stderr)

    observed_hashes = {
        "fixture_sha256": sha256_file(fixture),
        "oracle_sha256": sha256_file(oracle),
        "extractor_sha256": sha256_file(extract),
    }

    record: dict = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "completed",
        "argv": command,
        "env": canonical_env({key: env[key] for key in env if key.startswith("OCR_")}),
        "input": {"path": str(fixture), "sha256": hashes["fixture_sha256"], "dimensions": dimensions,
                  "binding": "authoritative-heic-fixture"},
        "oracle": {"path": str(oracle), "sha256": hashes["oracle_sha256"]},
        "extractor": {"path": str(extract), "sha256": hashes["extractor_sha256"]},
        "identity": {"preflight": hashes, "postrun": observed_hashes},
        "latency_ms": latency_ms,
        "peak_rss_mb": round(rss_mb(), 3),
        "returncode": completed.returncode,
        "request": {"base_url": EXPECTED_BASE_URL, "model": EXPECTED_MODEL},
    }

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        record.update(rejection("EXTRACTOR_OUTPUT_NOT_JSON", detail=str(error)))
        (out / "result.json").write_text(json.dumps(record, indent=2))
        return 1, record

    record["extractor_result"] = result
    audit = (result.get("provenance") or {}).get("audit")
    if not isinstance(audit, dict) or audit.get("input_sha256") != hashes["fixture_sha256"] or audit.get("extractor_source_sha256") != hashes["extractor_sha256"]:
        record.update(rejection("STALE_SOURCE_OR_ARTIFACT", expected=hashes, actual=audit))
        (out / "result.json").write_text(json.dumps(record, indent=2))
        (out / "manifest.json").write_text(json.dumps({"run_id": run_id, "argv": command, "env": record["env"], **hashes, "dimensions": dimensions}, indent=2))
        return 1, record
    if observed_hashes != hashes:
        record.update(rejection("IDENTITY_DRIFT", expected=hashes, actual=observed_hashes))
        (out / "result.json").write_text(json.dumps(record, indent=2))
        (out / "manifest.json").write_text(json.dumps({"run_id": run_id, "argv": command, "env": record["env"], **hashes, "dimensions": dimensions}, indent=2))
        return 1, record
    result_dimensions = (result.get("provenance") or {}).get("audit", {}).get("input_dimensions")
    if result_dimensions != dimensions:
        record.update(rejection("FIXTURE_DIMENSIONS_MISMATCH", expected=dimensions, actual=result_dimensions))
        (out / "result.json").write_text(json.dumps(record, indent=2))
        (out / "manifest.json").write_text(json.dumps({"run_id": run_id, "argv": command, "env": record["env"], **hashes, "dimensions": dimensions}, indent=2))
        return 1, record
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    record["route"] = meta.get("route")
    record["tool"] = result.get("tool")
    record["request_count"] = meta.get("request_count")
    record["load_duration"] = meta.get("load_duration")
    record["languages"] = sorted({language for note in json.loads(result.get("text", "{}")).get("notes", []) for language in note.get("languages", [])}) if isinstance(result.get("text"), str) else []

    if completed.returncode != 0:
        record.update(rejection("EXTRACTOR_NONZERO_EXIT", returncode=completed.returncode))
    elif result.get("status") != "ok":
        record.update(rejection("EXTRACTOR_STATUS_NOT_OK", extractor_status=result.get("status")))
    elif record["route"] != EXPECTED_ROUTE:
        record.update(rejection("ROUTE_MISMATCH", expected=EXPECTED_ROUTE, actual=record["route"]))
    elif record["tool"] != EXPECTED_TOOL:
        record.update(rejection("TOOL_MISMATCH", expected=EXPECTED_TOOL, actual=record["tool"]))
    elif record["request"].get("base_url") != EXPECTED_BASE_URL:
        record.update(rejection("REMOTE_OR_NONLOCAL_BASE_URL"))
    elif record["request_count"] is None or not 1 <= int(record["request_count"]) <= 8:
        record.update(rejection("REQUEST_COUNT_INVALID", actual=record["request_count"]))
    else:
        oracle_payload = json.loads(oracle.read_text())
        oracle_notes = oracle_payload.get("notes")
        if not isinstance(oracle_notes, list) or len(oracle_notes) != args.oracle_count:
            record.update(rejection("ORACLE_COUNT_MISMATCH", expected=args.oracle_count, actual=len(oracle_notes or [])))
        else:
            detections, _ = extract_detections(result)
            scored = score({"oracle": oracle_notes, "detections": detections, "image_size": dimensions}, args.iou)
            record["metrics"] = {
                key: scored[key]
                for key in ("true_positives", "false_positives", "false_negatives", "precision", "recall", "mean_matched_ocr_similarity")
            }
            record["metrics"]["oracle_notes"] = len(oracle_notes)
            record["metrics"]["languages"] = record["languages"]
            record["gates"] = {
                "route": True,
                "tool": True,
                "localhost": True,
                "oracle_count": True,
                "rss": record["peak_rss_mb"] < MAX_RSS_MB,
                "tp": scored["true_positives"] >= args.min_tp,
                "fp": scored["false_positives"] <= args.max_fp,
            }
            record["status"] = "accepted" if all(record["gates"].values()) else "rejected"
            if record["status"] == "rejected":
                record["rejection_code"] = "METRIC_GATE_FAILED"

    (out / "result.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    (out / "manifest.json").write_text(json.dumps({"run_id": run_id, "argv": command, "env": record["env"], **hashes, "dimensions": dimensions}, indent=2))
    return 0 if record["status"] == "accepted" else 1, record


def image_dimensions(path: Path, timeout: float = 2.0) -> list[int] | None:
    try:
        from PIL import Image
        from PIL import UnidentifiedImageError
    except ImportError:
        Image = None
        UnidentifiedImageError = ()

    if Image is not None:
        try:
            with Image.open(path) as image:
                return [image.width, image.height]
        except UnidentifiedImageError:
            pass

    if path.suffix.lower() not in {".heic", ".heif"} or sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=max(0.1, timeout), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"pixelWidth", "pixelHeight"}:
            try:
                values[key] = int(value.strip())
            except ValueError:
                pass
    if completed.returncode == 0 and set(values) == {"pixelWidth", "pixelHeight"}:
        return [values["pixelWidth"], values["pixelHeight"]]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--extract", type=Path, default=Path(__file__).parents[1] / "media" / "extract.py")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--oracle-count", type=int, default=19)
    parser.add_argument("--min-tp", type=int, default=1)
    parser.add_argument("--max-fp", type=int, default=0)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    try:
        code, record = run_once(args)
    except Exception as error:
        print(f"REJECTED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
