#!/usr/bin/env python3
"""Score generic sticky-note detections against an independent box/text oracle."""

from __future__ import annotations

import argparse
import difflib
import json
import re
from functools import lru_cache
from pathlib import Path


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def box_from_oracle(note: dict, image_size: tuple[float, float] | None) -> list[float]:
    box = note.get("box") or note.get("bbox") or note.get("bounding_box")
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError("oracle note requires box or bounding_box [4 values]")
    if note.get("box") is not None or note.get("bbox") is not None:
        return [float(value) for value in box]
    if not image_size:
        raise ValueError("normalized oracle boxes require image_size")
    width, height = image_size
    ymin, xmin, ymax, xmax = (float(value) for value in box)
    return [xmin * width, ymin * height, xmax * width, ymax * height]


def box_from_detection(note: dict, image_size: tuple[float, float] | None) -> list[float]:
    box = note.get("box") or note.get("bbox")
    if not isinstance(box, list) or len(box) != 4:
        return []
    values = [float(value) for value in box]
    if note.get("box") is not None:
        return values
    if not image_size:
        raise ValueError("normalized detection boxes require image_size")
    width, height = image_size
    return [values[0] * width, values[1] * height, values[2] * width, values[3] * height]


def validate_fixture_oracle_identity(payload: dict, oracle_override: list[dict] | None) -> None:
    fixture = payload.get("fixture")
    oracle = payload.get("oracle_manifest") or payload.get("oracle_ref")
    fixture_id = payload.get("fixture_id") or payload.get("fixture_sha256")
    oracle_id = payload.get("oracle_fixture_id") or payload.get("oracle_fixture_sha256") or payload.get("oracle_id")
    if fixture_id is not None and oracle_id is not None and str(fixture_id) != str(oracle_id):
        raise ValueError("fixture/oracle mismatch")
    if isinstance(fixture, dict) and isinstance(oracle, dict):
        fixture_id = fixture.get("id") or fixture.get("sha256") or fixture.get("path")
        oracle_fixture_id = oracle.get("fixture_id") or oracle.get("fixture_sha256")
        if fixture_id is not None and oracle_fixture_id is not None and str(fixture_id) != str(oracle_fixture_id):
            raise ValueError("fixture/oracle mismatch")


def iou(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def text_similarity(first: object, second: object) -> float:
    return difflib.SequenceMatcher(None, normalize_text(first), normalize_text(second)).ratio()


def optimal_iou_matches(oracle_boxes: list[list[float]], detections: list[dict], threshold: float) -> dict[int, int]:
    """Return one-to-one matches maximizing TP cardinality, then total IoU."""
    values = [[iou(expected, detection.get("box", [])) for detection in detections] for expected in oracle_boxes]

    @lru_cache(maxsize=None)
    def solve(oracle_index: int, used_mask: int) -> tuple[int, float, tuple[int | None, ...]]:
        if oracle_index == len(oracle_boxes):
            return 0, 0.0, ()
        best_count, best_score, best_tail = solve(oracle_index + 1, used_mask)
        best = (best_count, best_score, (None,) + best_tail)
        for detection_index, value in enumerate(values[oracle_index]):
            if value < threshold or used_mask & (1 << detection_index):
                continue
            tail_count, tail_score, tail = solve(oracle_index + 1, used_mask | (1 << detection_index))
            candidate = (tail_count + 1, value + tail_score, (detection_index,) + tail)
            if candidate[:2] > best[:2] or (candidate[:2] == best[:2] and tuple(-1 if x is None else x for x in candidate[2]) < tuple(-1 if x is None else x for x in best[2])):
                best = candidate
        return best

    _, _, assignment = solve(0, 0)
    return {oracle_index: detection_index for oracle_index, detection_index in enumerate(assignment) if detection_index is not None}


def score(payload: dict, threshold: float, oracle_override: list[dict] | None = None) -> dict:
    image_size = payload.get("image_size") or payload.get("imageSize")
    dimensions = tuple(float(value) for value in image_size) if image_size else None
    oracle = oracle_override if oracle_override is not None else (payload.get("oracle") or payload.get("notes"))
    detections = payload.get("detections", [])
    proposals = payload.get("proposals") or payload.get("proposal_detections") or []
    if not isinstance(oracle, list) or not isinstance(detections, list):
        raise ValueError("input requires oracle/notes and detections arrays")
    if not isinstance(proposals, list):
        raise ValueError("proposals must be an array")
    validate_fixture_oracle_identity(payload, oracle_override)

    oracle_boxes = [box_from_oracle(note, dimensions) for note in oracle]
    normalized_detections = [{**detection, "box": box_from_detection(detection, dimensions)} for detection in detections]
    matches = optimal_iou_matches(oracle_boxes, normalized_detections, threshold)
    proposal_matches = optimal_iou_matches(
        oracle_boxes,
        [{**proposal, "box": box_from_detection(proposal, dimensions)} for proposal in proposals],
        threshold,
    )
    notes = []
    for index, (note, expected_box) in enumerate(zip(oracle, oracle_boxes), 1):
        detection_index = matches.get(index - 1)
        best_iou = iou(expected_box, normalized_detections[detection_index]["box"]) if detection_index is not None else 0.0
        matched = detection_index is not None
        detection = normalized_detections[detection_index] if matched else None
        expected_text = note.get("text") or note.get("transcript") or note.get("verbatim_text") or ""
        notes.append({
            "note": index,
            "detection": detection_index + 1 if matched else None,
            "iou": round(best_iou, 6),
            "matched": matched,
            "ocr_similarity": round(text_similarity(detection.get("text", "") if detection else "", expected_text), 6),
        })

    true_positives = sum(note["matched"] for note in notes)
    false_positives = len(detections) - true_positives
    false_negatives = len(oracle) - true_positives
    matched_ocr = [note["ocr_similarity"] for note in notes if note["matched"]]
    return {
        "iou_threshold": threshold,
        "detections": len(detections),
        "oracle_notes": len(oracle),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": true_positives / len(detections) if detections else 0.0,
        "recall": true_positives / len(oracle) if oracle else 0.0,
        "mean_matched_ocr_similarity": sum(matched_ocr) / len(matched_ocr) if matched_ocr else 0.0,
        "proposal_count": len(proposals),
        "proposal_true_positives": len(proposal_matches),
        "proposal_false_negatives": len(oracle) - len(proposal_matches),
        "proposal_recall": len(proposal_matches) / len(oracle) if oracle else 0.0,
        "notes": notes,
        "latency_ms": payload.get("latency_ms", payload.get("latencyMs")),
        "peak_rss_mb": payload.get("peak_rss_mb"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=Path)
    parser.add_argument("--oracle", type=Path, help="Separate oracle JSON containing a notes array")
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--proposal-recall-min", type=int, default=12)
    parser.add_argument("--final-tp-min", type=int, default=10)
    parser.add_argument("--prior-fp", type=int, default=None)
    parser.add_argument("--request-count", type=int, default=None)
    parser.add_argument("--max-requests", type=int, default=2)
    parser.add_argument("--privacy", default=None)
    parser.add_argument("--geometry-authority", default=None)
    args = parser.parse_args()
    if not 0 < args.iou <= 1:
        parser.error("--iou must be in (0, 1]")
    payload = json.loads(args.payload.read_text())
    oracle = json.loads(args.oracle.read_text())["notes"] if args.oracle else None
    result = score(payload, args.iou, oracle)
    result["gates"] = {
        "proposal_recall": {
            "pass": result["proposal_true_positives"] >= args.proposal_recall_min,
            "matched": result["proposal_true_positives"],
            "required": args.proposal_recall_min,
            "denominator": result["oracle_notes"],
            "recall": result["proposal_recall"],
        },
        "final_tp": {
            "pass": result["true_positives"] >= args.final_tp_min,
            "actual": result["true_positives"],
            "required": args.final_tp_min,
        },
        "final_fp": {
            "pass": args.prior_fp is None or result["false_positives"] <= args.prior_fp + 2,
            "actual": result["false_positives"],
            "prior": args.prior_fp,
            "max": None if args.prior_fp is None else args.prior_fp + 2,
        },
        "requests": {
            "pass": args.request_count is not None and 0 <= args.request_count <= args.max_requests,
            "actual": args.request_count,
            "max": args.max_requests,
        },
        "privacy": {
            "pass": args.privacy == "local-only" or args.privacy == "localhost-only",
            "actual": args.privacy,
            "required": "local-only or localhost-only",
        },
        "geometry": {
            "pass": args.geometry_authority == "local",
            "actual": args.geometry_authority,
            "required": "local",
        },
    }
    result["gates"]["all"] = all(gate["pass"] for gate in result["gates"].values())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
