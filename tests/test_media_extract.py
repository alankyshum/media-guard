import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


EXTRACT_PATH = Path(__file__).parents[1] / "media" / "extract.py"
SPEC = importlib.util.spec_from_file_location("media_extract", EXTRACT_PATH)
extract = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(extract)


class MediaExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = extract.CACHE_DIR
        extract.CACHE_DIR = self.tmp.name
        extract.CACHE_TTL_SECONDS = 86400

    def tearDown(self):
        extract.CACHE_DIR = self.cache_dir
        self.tmp.cleanup()

    def test_cache_success_is_content_addressed_and_versioned(self):
        source = Path(self.tmp.name) / "sample.txt"
        source.write_text("stable OCR")
        key = extract.cache_key(str(source), "text", "base", 100)
        result = {"status": "ok", "text": "stable OCR"}
        extract.cache_put(key, result)
        self.assertEqual(extract.cache_get(key), result)
        envelope = json.loads((Path(self.tmp.name) / f"{key}.json").read_text())
        self.assertEqual(envelope["cache_version"], extract.CACHE_VERSION)

        source.write_text("changed OCR")
        self.assertIsNone(extract.cache_get(extract.cache_key(str(source), "text", "base", 100)))

    def test_cache_corruption_and_expiry_are_misses(self):
        source = Path(self.tmp.name) / "sample.txt"
        source.write_text("text")
        key = extract.cache_key(str(source), "text", "base", 100)
        cache_path = Path(self.tmp.name) / f"{key}.json"
        cache_path.write_text("not json")
        self.assertIsNone(extract.cache_get(key))

        extract.cache_put(key, {"status": "ok"})
        envelope = json.loads(cache_path.read_text())
        envelope["cached_at"] = time.time() - 10
        cache_path.write_text(json.dumps(envelope))
        extract.CACHE_TTL_SECONDS = 1
        self.assertIsNone(extract.cache_get(key))
        self.assertFalse(cache_path.exists())

    def test_cache_is_bounded(self):
        source = Path(self.tmp.name) / "sample.txt"
        source.write_text("text")
        old_limit = extract.CACHE_MAX_ENTRIES
        extract.CACHE_MAX_ENTRIES = 1
        try:
            extract.cache_put("first", {"status": "ok"})
            time.sleep(0.01)
            extract.cache_put("second", {"status": "ok"})
            self.assertLessEqual(len(list(Path(self.tmp.name).glob("*.json"))), 1)
        finally:
            extract.CACHE_MAX_ENTRIES = old_limit

    def test_correction_accepts_only_provenanced_exact_spans(self):
        text = "Total 1O0 USD"
        payload = {
            "corrections": [
                {"source": "1O0", "replacement": "100", "start": 6,
                 "confidence": 0.99, "provenance": {"page": 1, "bbox": [1, 2, 3, 4]}},
                {"source": "USD", "replacement": "EUR", "start": 10,
                 "confidence": 0.50, "provenance": {}},
            ]
        }
        with patch.object(extract, "_correction_enabled", return_value=True), \
                patch.object(extract.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"message": {"content": json.dumps(payload)}}).encode()
            corrected, meta = extract.correct_ocr_text(text, 1)
            request = urlopen.call_args.args[0]
            body = json.loads(request.data)
            self.assertTrue(request.full_url.endswith("/api/chat"))
            self.assertEqual(body["format"]["required"], ["corrections"])
            self.assertFalse(body["think"])
        self.assertEqual(corrected, "Total 100 USD")
        self.assertEqual(meta["status"], "corrected")
        self.assertEqual(len(meta["accepted_spans"]), 1)

    def test_correction_timeout_returns_original_text(self):
        with patch.object(extract, "_correction_enabled", return_value=True), \
                patch.object(extract.urllib.request, "urlopen", side_effect=TimeoutError):
            corrected, meta = extract.correct_ocr_text("deterministic", 0.01)
        self.assertEqual(corrected, "deterministic")
        self.assertEqual(meta["status"], "timeout")

    def test_correction_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                extract.urllib.request, "urlopen") as urlopen:
            corrected, meta = extract.correct_ocr_text("deterministic", 0.01)
        self.assertEqual(corrected, "deterministic")
        self.assertEqual(meta["status"], "disabled")
        urlopen.assert_not_called()

    def test_cache_key_separates_correction_mode(self):
        source = Path(self.tmp.name) / "sample.txt"
        source.write_text("same bytes")
        disabled = extract.cache_key(str(source), "text", "base", 100, False)
        enabled = extract.cache_key(str(source), "text", "base", 100, True)
        self.assertNotEqual(disabled, enabled)

    def test_cache_key_includes_image_preprocessing_version(self):
        source = Path(self.tmp.name) / "sample.png"
        source.write_bytes(b"same bytes")
        with patch.object(extract, "IMAGE_PREPROCESS_VERSION", "v-test-a"):
            first = extract.cache_key(str(source), "image", "base", 100, False)
        with patch.object(extract, "IMAGE_PREPROCESS_VERSION", "v-test-b"):
            second = extract.cache_key(str(source), "image", "base", 100, False)
        self.assertNotEqual(first, second)

    def test_cache_key_binds_resident_route_context(self):
        source = Path(self.tmp.name) / "resident.png"
        source.write_bytes(b"same bytes")
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "0"}), \
                patch.object(extract, "OCR_VLM_BASE_URL", "http://127.0.0.1:11434"), \
                patch.object(extract, "OCR_VLM_MODEL", "model-a"), \
                patch.object(extract, "STICKY_RESIDENT_VERSION", "route-a"), \
                patch.object(extract, "STICKY_RESIDENT_KEEP_ALIVE", "10m"):
            disabled = extract.cache_key(str(source), "image", "base", 100)
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}), \
                patch.object(extract, "OCR_VLM_BASE_URL", "http://127.0.0.1:11435"), \
                patch.object(extract, "OCR_VLM_MODEL", "model-b"), \
                patch.object(extract, "STICKY_RESIDENT_VERSION", "route-b"), \
                patch.object(extract, "STICKY_RESIDENT_KEEP_ALIVE", "0"):
            enabled = extract.cache_key(str(source), "image", "base", 100)
        self.assertNotEqual(disabled, enabled)

    def test_warm_cache_resident_mode_transitions_miss(self):
        source = Path(self.tmp.name) / "transition.png"
        source.write_bytes(b"same bytes")
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "0"}):
            disabled_key = extract.cache_key(str(source), "image", "base", 100)
        extract.cache_put(disabled_key, {"mode": "disabled"})
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}):
            enabled_key = extract.cache_key(str(source), "image", "base", 100)
        self.assertNotEqual(disabled_key, enabled_key)
        self.assertIsNone(extract.cache_get(enabled_key))

        reverse_source = Path(self.tmp.name) / "reverse-transition.png"
        reverse_source.write_bytes(b"reverse bytes")
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}):
            enabled_key = extract.cache_key(str(reverse_source), "image", "base", 100)
        extract.cache_put(enabled_key, {"mode": "enabled"})
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "0"}):
            disabled_key = extract.cache_key(str(reverse_source), "image", "base", 100)
        self.assertNotEqual(enabled_key, disabled_key)
        self.assertIsNone(extract.cache_get(disabled_key))

    def test_video_crv_route_returns_keyframes_and_transcript(self):
        source = Path(self.tmp.name) / "clip.mp4"
        source.write_bytes(b"video")
        frame = Path(self.tmp.name) / "frame-001.jpg"
        frame.write_bytes(b"frame")
        audio = Path(self.tmp.name) / "audio.m4a"
        audio.write_bytes(b"audio")
        commands = []

        def run(command, timeout):
            commands.append(command)
            return 0, "", ""

        with patch.object(extract, "which", side_effect=lambda name: "/usr/local/bin/crv" if name == "crv" else None), \
                patch.object(extract, "run", side_effect=run), \
                patch.object(extract.os, "walk", return_value=[(self.tmp.name, [], [frame.name, audio.name])]), \
                patch.object(extract, "transcribe_via_skill", return_value=("spoken words", "transcribe-skill", {})):
            # crv writes into its output directory; expose fixture files there.
            with patch.object(extract.tempfile, "mkdtemp", return_value=self.tmp.name):
                text, tool, meta = extract.extract_video(str(source), "base", 10)

        self.assertEqual(text, "spoken words")
        self.assertEqual(tool, "crv+transcribe-skill")
        self.assertEqual(meta["frames"], [str(frame)])
        self.assertEqual(meta["frame_count"], 1)
        self.assertEqual(meta["audio"], str(audio))
        self.assertEqual(commands[0][0], "crv")

    def test_video_ffmpeg_fallback_preserves_transcript_schema_without_frames(self):
        source = Path(self.tmp.name) / "clip.mp4"
        source.write_bytes(b"video")
        commands = []

        def run(command, timeout):
            commands.append(command)
            Path(command[-1]).write_bytes(b"wav")
            return 0, "", ""

        with patch.object(extract, "which", side_effect=lambda name: "/usr/local/bin/ffmpeg" if name == "ffmpeg" else None), \
                patch.object(extract, "run", side_effect=run), \
                patch.object(extract, "transcribe_via_skill", return_value=("fallback transcript", "transcribe-skill", {})):
            text, tool, meta = extract.extract_video(str(source), "base", 10)

        self.assertEqual(text, "fallback transcript")
        self.assertEqual(tool, "ffmpeg+transcribe-skill")
        self.assertEqual(meta["frames"], [])
        self.assertEqual(meta["degraded"], "crv-unavailable")
        self.assertEqual(commands[0][0], "ffmpeg")

    def test_image_derivative_is_grayscale_upscaled_and_source_preserved(self):
        from PIL import Image, ExifTags

        source = Path(self.tmp.name) / "rotated.png"
        image = Image.new("RGB", (40, 20), "white")
        image.save(source)
        original = source.read_bytes()
        derivative, meta = extract._image_derivative(str(source), self.tmp.name, 90)
        self.assertEqual(source.read_bytes(), original)
        with Image.open(derivative) as result:
            self.assertEqual(result.mode, "L")
            self.assertEqual(result.size, (40, 80))
        self.assertTrue(meta["upscaled"])
        self.assertEqual(meta["angle"], 90)

    def test_classify_supports_heic(self):
        self.assertEqual(extract.classify("note.heic", None, "auto"), "image")
        self.assertEqual(extract.classify("note.heif", None, "auto"), "image")

    def test_sticky_note_detector_returns_colored_regions_not_background(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "notes.png"
        image = Image.new("RGB", (1000, 700), (35, 40, 48))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 100, 390, 350), fill=(245, 220, 70))
        draw.rectangle((600, 280, 900, 570), fill=(80, 190, 220))
        image.save(source)
        regions, meta = extract._note_regions(str(source), self.tmp.name)
        self.assertEqual(meta["candidate_count"], 2)
        self.assertEqual(len(regions), 2)
        self.assertEqual([r["color"] for r in regions], ["yellow", "cyan"])

    def test_sticky_note_detector_rejects_thin_and_oversized_color_regions(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "background.png"
        image = Image.new("RGB", (1000, 700), (35, 40, 48))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 999, 30), fill=(245, 80, 80))
        draw.rectangle((0, 80, 999, 650), fill=(80, 190, 220))
        image.save(source)
        regions, meta = extract._note_regions(str(source), self.tmp.name)
        self.assertEqual(meta["candidate_count"], 0)
        self.assertEqual(regions, [])

    def test_sticky_note_ensemble_keeps_adjacent_color_regions_separate(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "adjacent.png"
        image = Image.new("RGB", (1000, 600), (30, 30, 35))
        draw = ImageDraw.Draw(image)
        draw.rectangle((80, 120, 430, 430), fill=(245, 220, 70))
        draw.rectangle((440, 120, 790, 430), fill=(80, 190, 220))
        image.save(source)
        regions, meta = extract._note_regions(str(source), self.tmp.name)
        self.assertEqual(len(regions), 2)
        self.assertEqual([region["color"] for region in regions], ["yellow", "cyan"])

    def test_sticky_note_nms_removes_overlapping_shadow_proposal(self):
        candidates = [
            {"box": [100, 100, 400, 350], "colorfulness": 0.8, "rectangle_confidence": 0.9, "confidence": 90, "area_fraction": 0.1},
            {"box": [112, 112, 412, 362], "colorfulness": 0.1, "edge_density": 0.2, "area_fraction": 0.1},
        ]
        for candidate in candidates:
            candidate["candidate_score"] = extract._candidate_score(candidate)
        selected = extract._nms_regions(candidates)
        self.assertEqual([candidate["box"] for candidate in selected], [[100, 100, 400, 350]])

    def test_sticky_note_nms_preserves_grid_adjacent_boxes(self):
        candidates = [
            {"box": [0, 0, 100, 100], "colorfulness": 0.8, "confidence": 90, "area_fraction": 0.1},
            {"box": [100, 0, 200, 100], "colorfulness": 0.7, "confidence": 80, "area_fraction": 0.1},
        ]
        for candidate in candidates:
            candidate["candidate_score"] = extract._candidate_score(candidate)
        selected = extract._nms_regions(candidates)
        self.assertEqual(len(selected), 2)

    def test_sticky_note_globalizes_local_proposal_with_provenance(self):
        region = extract._globalize_region(
            {"box": [10, 20, 50, 80], "coordinate_frame": "local", "source_size": [100, 200]},
            (1000, 1000),
        )
        self.assertEqual(region["box"], [100, 100, 500, 400])
        self.assertEqual(region["coordinate_frame"], "global")
        self.assertEqual(region["source_frame"], "local")
        self.assertIn("globalized", region["provenance"])

    def test_tiled_proposal_restores_global_coordinates_across_tile_boundary(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "tiled.png"
        image = Image.new("RGB", (1600, 600), (30, 30, 35))
        ImageDraw.Draw(image).rectangle((735, 140, 980, 420), fill=(245, 220, 70))
        image.save(source)
        with patch.object(extract, "NOTE_TILED_PROPOSALS", True), patch.object(extract, "NOTE_TILE_SIZE", 800), patch.object(extract, "NOTE_TILE_OVERLAP", 0.25):
            proposals = extract._tiled_note_regions(str(source))
        self.assertTrue(proposals)
        self.assertTrue(any(item["box"][0] >= 700 and item["box"][2] > 900 for item in proposals))
        self.assertTrue(all(item["coordinate_frame"] == "global" for item in proposals))
        self.assertTrue(all("tile" in item["provenance"] and "overlap" in item["provenance"] for item in proposals))

    def test_tiled_proposals_dedupe_overlap_but_preserve_grid_neighbors_and_cap(self):
        candidates = [
            {"box": [0, 0, 100, 100], "colorfulness": 0.8, "provenance": ["tile"], "area_fraction": 0.1},
            {"box": [2, 2, 102, 102], "colorfulness": 0.7, "provenance": ["tile", "overlap"], "area_fraction": 0.1},
            {"box": [100, 0, 200, 100], "colorfulness": 0.7, "provenance": ["tile"], "area_fraction": 0.1},
        ]
        for candidate in candidates:
            candidate["candidate_score"] = extract._candidate_score(candidate)
        selected = extract._nms_regions(candidates, max_regions=2)
        self.assertEqual([item["box"] for item in selected], [[0, 0, 100, 100], [100, 0, 200, 100]])

    def test_tiled_proposals_include_apple_rectangles_in_global_frame(self):
        from PIL import Image

        source = Path(self.tmp.name) / "apple-tiled.png"
        Image.new("RGB", (1600, 600), (30, 30, 35)).save(source)
        calls = []

        def rectangles(path, timeout):
            calls.append((path, timeout))
            return ([{"box": [100, 120, 400, 420], "color": "yellow", "colorfulness": 0.8,
                       "geometry": {"rectangleConfidence": 0.9, "areaFraction": 0.2}}], {})

        with patch.object(extract, "NOTE_TILE_SIZE", 800), patch.object(extract, "NOTE_TILE_OVERLAP", 0.25), \
                patch.object(extract.sys, "platform", "darwin"), patch.object(extract, "_run_apple_vision_rectangles", side_effect=rectangles):
            proposals = extract._tiled_note_regions(str(source), timeout=2)

        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(proposals)
        self.assertTrue(all(item["coordinate_frame"] == "global" for item in proposals))
        self.assertTrue(any(item["box"] == [100, 120, 400, 420] for item in proposals))
        self.assertTrue(all(item["source_frame"] == "tile" for item in proposals))

    def test_tiled_proposal_source_is_disabled_by_default(self):
        self.assertFalse(extract.NOTE_TILED_PROPOSALS)

    def test_sticky_note_split_recovers_multiple_edge_valleys(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "multi-split.png"
        image = Image.new("L", (900, 300), 30)
        draw = ImageDraw.Draw(image)
        for left in (40, 320, 600):
            draw.rectangle((left, 40, left + 220, 260), fill=220)
        image.save(source)
        candidate = {"box": [0, 0, 900, 300], "colorfulness": 0.5, "area_fraction": 0.9, "provenance": ["edge"]}
        candidate["candidate_score"] = extract._candidate_score(candidate)
        parts = extract._split_oversized_regions(str(source), [candidate])
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all("edge-valley-split" in part["provenance"] for part in parts))

    def test_sticky_note_pale_contrast_requires_edge_support(self):
        from PIL import Image

        source = Path(self.tmp.name) / "pale.png"
        Image.new("L", (100, 100), 240).save(source)
        candidate = {"box": [0, 0, 100, 100], "color": "neutral", "provenance": ["contrast"], "edge_density": 0.01, "area_fraction": 0.1}
        with patch.object(extract, "_contrast_regions", return_value=[candidate]), patch.object(extract, "_note_regions", return_value=([], {})), patch.object(extract, "_edge_regions", return_value=[]):
            self.assertEqual(extract._physical_note_regions(str(source)), [])

    def test_sticky_note_edge_proposals_are_bounded_and_non_note_is_rejected(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "edges.png"
        image = Image.new("RGB", (1600, 1000), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 180, 800, 700), outline=(100, 100, 100), width=8)
        draw.rectangle((0, 0, 1599, 40), fill=(240, 50, 50))
        image.save(source)
        proposals = extract._edge_regions(str(source))
        self.assertLessEqual(len(proposals), extract.NOTE_MAX_REGIONS)
        self.assertTrue(all(0.18 <= (box[2] - box[0]) / max(box[3] - box[1], 1) <= 5.5 for box in (item["box"] for item in proposals)))

    def test_sticky_note_crop_ocr_is_bounded_and_cleans_up(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "notes.png"
        image = Image.new("RGB", (800, 500), (25, 25, 25))
        ImageDraw.Draw(image).rectangle((80, 80, 500, 420), fill=(245, 220, 70))
        image.save(source)
        seen = []

        def candidate(path, timeout, psm=11):
            seen.append(path)
            return "keep this note", 91.0

        with patch.object(extract, "_run_sticky_vlm", return_value=(None, {"why": "test"})), patch.object(
                extract, "which", return_value="tesseract"), patch.object(
                extract, "_ocr_candidate", side_effect=candidate), patch.object(
                extract, "NOTE_MAX_REGIONS", 1), patch.object(
                extract, "NOTE_MAX_CROP_PIXELS", 10000):
            text, tool, meta = extract.extract_image(str(source), 2, "sticky-notes")
        self.assertEqual(tool, "sticky-note-crop-ocr")
        self.assertIn("keep this note", text)
        self.assertEqual(meta["recognized_notes"], 1)
        self.assertFalse(Path(seen[0]).exists())

    def test_sticky_note_engine_skips_full_image_ocr(self):
        source = Path(self.tmp.name) / "notes.png"
        from PIL import Image
        Image.new("RGB", (800, 500), (30, 30, 30)).save(source)
        with patch.object(extract, "_document_image", side_effect=AssertionError("full-image OCR path")), patch.object(
                extract, "_run_tesseract", side_effect=AssertionError("full-image OCR path")):
            extract.extract_image(str(source), 1, "sticky-notes")

    def test_sticky_note_schema_normalizes_pixel_boxes_and_rejects_extra_fields(self):
        payload = {"notes": [{"id": "1", "bbox": [100, 200, 300, 400], "color": "blue", "content": "你好", "confidence": 0.8, "languages": ["zh-Hant"]}]}
        normalized = extract._validate_sticky_notes(payload, (1000, 1000))
        self.assertEqual(normalized["notes"][0]["bbox"], [0.1, 0.2, 0.3, 0.4])
        self.assertIsNone(extract._validate_sticky_notes({**payload, "extra": True}, (1000, 1000)))

    def test_sticky_note_model_aliases_canonicalize_before_strict_validation(self):
        payload = {"notes": [{"box": [100, 200, 300, 400], "text": "你好"}]}
        normalized = extract._validate_sticky_notes(payload, (1000, 1000))
        self.assertEqual(normalized["notes"][0]["bbox"], [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(normalized["notes"][0]["content"], "你好")

    def test_multicrop_accepts_empty_and_presence_text_only(self):
        self.assertEqual(extract._validate_sticky_multicrop({"notes": []}), {"findings": []})
        payload = {"notes": [{"content": "observed", "confidence": 0.8, "languages": ["en"]}]}
        self.assertEqual(extract._validate_sticky_multicrop(payload)["findings"][0]["content"], "observed")

    def test_multicrop_rejects_geometry_identity_color_and_unknown_fields(self):
        base = {"content": "x", "confidence": 1, "languages": []}
        for field, value in (("bbox", [0, 0, 1, 1]), ("id", "n"), ("color", "yellow"), ("extra", True)):
            diagnostics = []
            self.assertIsNone(extract._validate_sticky_multicrop({"notes": [{**base, field: value}]}, diagnostics=diagnostics))
            self.assertIn("unknown-field", diagnostics[0])

    def test_multicrop_rejects_multiple_and_invalid_presence_text(self):
        valid = {"content": "x", "confidence": 1, "languages": []}
        self.assertIsNone(extract._validate_sticky_multicrop({"notes": [valid, valid]}))
        for item in ({**valid, "content": ""}, {**valid, "confidence": 2}, {**valid, "languages": [1]}):
            self.assertIsNone(extract._validate_sticky_multicrop({"notes": [item]}))

    def test_multicrop_reports_bounded_field_predicates_without_content(self):
        payload = {"notes": [{"content": "", "confidence": 2, "languages": [1]}]}
        diagnostics = []
        self.assertIsNone(extract._validate_sticky_multicrop(payload, (100, 100), diagnostics))
        self.assertEqual(diagnostics, ["notes[1].content.nonempty-string"])
        diagnostics = []
        malformed = {"notes": [{"content": "x", "confidence": 2, "languages": []}]}
        self.assertIsNone(extract._validate_sticky_multicrop(malformed, (100, 100), diagnostics))
        self.assertEqual(diagnostics, ["notes[1].confidence.range-0-1"])

    def test_multicrop_rejects_findings_wrapper_aliases(self):
        self.assertIsNone(extract._validate_sticky_multicrop({"findings": [{"text": "observed", "confidence": 0.8, "languages": []}]}))

    def test_cli_artifact_binds_input_and_extractor_audit_fields(self):
        source = Path(self.tmp.name) / "audit.txt"
        source.write_text("audit")
        with patch.object(sys, "argv", ["extract.py", str(source), "--kind", "text", "--no-cache"]), redirect_stdout(StringIO()) as stdout:
            extract.main()
        audit = json.loads(stdout.getvalue())["provenance"]["audit"]
        self.assertEqual(audit["input_bytes"], 5)
        self.assertEqual(len(audit["input_sha256"]), 64)
        self.assertEqual(audit["input_mime"], "text/plain")
        self.assertEqual(audit["extractor_source_path"], str(Path(extract.__file__).resolve()))
        self.assertEqual(len(audit["extractor_source_sha256"]), 64)

    def test_cli_audit_only_returns_current_source_audit_without_inference(self):
        source = Path(self.tmp.name) / "audit.txt"
        source.write_text("audit")
        with patch.object(sys, "argv", ["extract.py", str(source), "--kind", "text", "--audit-only"]), redirect_stdout(StringIO()) as stdout:
            extract.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["tool"], "audit")
        self.assertTrue(result["provenance"]["audit_only"])

    def test_sticky_note_vlm_uses_one_primary_request_and_strict_schema(self):
        source = Path(self.tmp.name) / "note.png"
        from PIL import Image
        Image.new("RGB", (100, 100), "yellow").save(source)
        response = {"message": {"content": json.dumps({"assignments": [{"id": "sheet-1", "color": "yellow", "content": "記住", "confidence": 0.9, "languages": ["zh-Hant"]}]})}}
        regions = [{"box": [0, 0, 100, 100], "color": "yellow"}]
        with patch.dict(os.environ, {"OCR_VLM_ENABLED": "1"}), patch.object(extract, "_ollama_json", return_value=response) as call:
            notes, meta = extract._run_sticky_vlm(str(source), "qwen2.5vl:32b", 2, regions)
        self.assertEqual(len(notes["assignments"]), 1)
        self.assertEqual(meta["model"], "qwen2.5vl:32b")
        self.assertEqual(call.call_count, 1)

    def test_resident_sticky_route_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(extract._sticky_resident_enabled())

    def test_resident_discovery_and_verification_are_strict_and_two_turn(self):
        from PIL import Image

        source = Path(self.tmp.name) / "resident.png"
        Image.new("RGB", (400, 300), "yellow").save(source)
        regions = [{"box": [20, 30, 180, 200], "color": "yellow"}]
        responses = [
            ({"sheets": [{"id": "sheet-1", "bbox": [0, 0, 1, 1], "color": "yellow"}]}, {"load_duration": 10}),
            ({"operations": [{"op": "keep", "ids": ["sheet-1"], "content": "keep", "confidence": 0.9, "languages": ["en"]}]}, {"eval_duration": 20}),
        ]

        def request(_path, _prompt, _schema, _timeout):
            parsed, metadata = responses.pop(0)
            return parsed, metadata

        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=regions), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=request) as call:
            text, tool, meta = extract._extract_sticky_notes(str(source), 5, {})

        result = json.loads(text)
        self.assertEqual(tool, "qwen2.5vl-resident-indexed")
        self.assertEqual(result["notes"][0]["bbox"], [0.05, 0.1, 0.45, 0.666667])
        self.assertEqual(meta["request_count"], 2)
        self.assertEqual(meta["state"], "indexed-verification")
        self.assertEqual(call.call_count, 2)

    def test_resident_turn_two_receives_all_proposals_when_discovery_underrecalls(self):
        from PIL import Image

        source = Path(self.tmp.name) / "resident-underrecall.png"
        Image.new("RGB", (400, 300), "yellow").save(source)
        regions = [
            {"box": [0, 0, 100, 100], "color": "yellow"},
            {"box": [200, 100, 300, 200], "color": "cyan"},
        ]
        prompts = []
        responses = [
            ({"sheets": [{"id": "sheet-1", "bbox": [0, 0, 1, 1], "color": "yellow"}]}, {}),
            ({"operations": [
                {"op": "keep", "ids": ["sheet-1"], "content": "first", "confidence": 0.9, "languages": ["en"]},
                {"op": "keep", "ids": ["sheet-2"], "content": "second", "confidence": 0.9, "languages": ["en"]},
            ]}, {}),
        ]

        def request(_path, prompt, _schema, _timeout):
            prompts.append(prompt)
            return responses.pop(0)

        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=regions), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=request):
            text, tool, meta = extract._extract_sticky_notes(str(source), 5, {})

        result = json.loads(text)
        self.assertEqual(tool, "qwen2.5vl-resident-indexed")
        self.assertEqual([note["content"] for note in result["notes"]], ["first", "second"])
        self.assertIn('"sheet-1","sheet-2"', prompts[1])
        self.assertEqual(meta["diagnostics"]["discovery"]["returned_ids"], ["sheet-1"])
        self.assertTrue(meta["diagnostics"]["verifier"]["discovery_ids_not_gating"])

    def test_resident_route_rejects_invalid_ids_and_falls_back(self):
        from PIL import Image

        source = Path(self.tmp.name) / "resident-invalid.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        regions = [{"box": [0, 0, 100, 100], "color": "yellow"}]
        responses = [({"sheets": [{"id": "invented", "bbox": [0, 0, 1, 1], "color": "yellow"}]}, {})]
        with patch.dict(os.environ, {"OCR_STICKY_RESIDENT_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=regions), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=lambda *args: responses.pop(0)), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[1], "local")

    def test_resident_schemas_reject_extra_fields_duplicate_ids_and_unknown_operations(self):
        self.assertIsNone(extract._validate_sticky_discovery({"sheets": [{"id": "a", "bbox": [0, 0, 1, 1], "color": "yellow", "extra": 1}]}))
        self.assertIsNone(extract._validate_sticky_discovery({"sheets": [{"id": "a", "bbox": [0, 0, 1, 1], "color": "yellow"}, {"id": "a", "bbox": [0, 0, 1, 1], "color": "yellow"}]}))
        self.assertIsNone(extract._validate_sticky_verification({"operations": [{"op": "invent", "ids": ["a"], "content": "x", "confidence": 1, "languages": []}]}, {"a"}))

    def test_resident_request_includes_keep_alive_and_load_metadata(self):
        source = Path(self.tmp.name) / "request.png"
        source.write_bytes(b"image")
        response = {"message": {"content": '{"sheets":[]}'}, "load_duration": 123, "total_duration": 456}
        with patch.dict(os.environ, {"OCR_VLM_ENABLED": "1"}), patch.object(extract, "_ollama_json", return_value=response) as call:
            parsed, meta = extract._run_sticky_resident_request(str(source), "prompt", extract.STICKY_DISCOVERY_SCHEMA, 1)
        body = call.call_args.args[1]
        self.assertEqual(parsed, {"sheets": []})
        self.assertEqual(body["keep_alive"], "10m")
        self.assertEqual(meta["load_duration"], 123)
        self.assertEqual(meta["total_duration"], 456)

    def test_multicrop_is_off_by_default_and_crop_order_is_bounded(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(extract._sticky_multicrop_enabled())
        from PIL import Image

        source = Path(self.tmp.name) / "crop-order.png"
        Image.new("RGB", (400, 100), "yellow").save(source)
        proposals = [
            {"box": [20, 10, 100, 80], "color": "yellow", "provenance": ["color"], "source_size": [400, 100]},
            {"box": [140, 10, 220, 80], "color": "cyan", "provenance": ["edge"], "source_size": [400, 100]},
            {"box": [260, 10, 340, 80], "color": "blue", "provenance": ["rectangle"], "source_size": [400, 100]},
            {"box": [360, 10, 399, 80], "color": "red", "provenance": ["color"], "source_size": [400, 100]},
        ]
        with patch.object(extract, "_physical_note_regions", return_value=proposals), \
                patch.object(extract, "STICKY_MULTICROP_MAX_REQUESTS", 3):
            regions = extract._multicrop_regions(str(source))
        self.assertEqual([region["id"] for region in regions], ["proposal-1", "proposal-2", "proposal-3"])
        self.assertEqual([region["box"] for region in regions], [item["box"] for item in proposals[:3]])
        self.assertEqual(len(regions), 3)

    def test_multicrop_resident_route_maps_global_boxes_and_merges_overlap(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop.png"
        Image.new("RGB", (400, 100), "yellow").save(source)
        responses = [
            ({"notes": [{"content": "same note", "confidence": 0.7, "languages": ["en"]}]}, {}),
            ({"notes": [{"content": "same note", "confidence": 0.9, "languages": ["en"]}]}, {}),
            ({"notes": [{"content": "other note", "confidence": 0.8, "languages": ["en"]}]}, {}),
        ]
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=[
                    {"box": [0, 0, 200, 100], "color": "yellow", "provenance": ["color"], "source_size": [400, 100]},
                    {"box": [100, 0, 300, 100], "color": "yellow", "provenance": ["color"], "source_size": [400, 100]},
                    {"box": [200, 0, 400, 100], "color": "yellow", "provenance": ["color"], "source_size": [400, 100]},
                ]), \
                patch.object(extract, "STICKY_MULTICROP_MAX_REQUESTS", 3), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=lambda *args: responses.pop(0)) as call:
            text, tool, meta = extract._extract_sticky_notes(str(source), 5, {})
        result = json.loads(text)
        self.assertEqual(tool, "qwen2.5vl-resident-multicrop")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(meta["request_count"], 3)
        self.assertEqual([note["content"] for note in result["notes"]], ["same note", "other note"])
        self.assertEqual(result["notes"][0]["bbox"], [0.25, 0.0, 0.75, 1.0])
        self.assertEqual(result["notes"][0]["id"], "proposal-2")
        mapping = meta["candidate_mapping"][0]
        self.assertEqual(mapping["id"], result["notes"][0]["id"])
        self.assertEqual(mapping["candidate_ids"], ["proposal-2", "proposal-1"])
        self.assertIn("dedupe-merge", mapping["provenance"])

    def test_multicrop_invalid_response_fails_closed(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop-invalid.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_run_sticky_resident_request", return_value=({"findings": [{"content": "x", "confidence": 2, "languages": []}]}, {})), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[1], "local")

    def test_multicrop_request_exception_reports_attempted_request(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop-request-error.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=[{"box": [0, 0, 80, 80], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]}]), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=OSError("connection refused")), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[2]["request_count"], 1)
        self.assertEqual(result[2]["failure_class"], "request-exception")
        self.assertIn("connection refused", result[2]["failure_reason"])

    def test_multicrop_crop_failure_reports_no_request(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop-crop-error.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=[{"box": [0, 0, 80, 80], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]}]), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=AssertionError("request made")), \
                patch.object(Image.Image, "crop", side_effect=OSError("crop failed")), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[2]["request_count"], 0)
        self.assertEqual(result[2]["failure_class"], "crop")
        self.assertIn("crop failed", result[2]["failure_reason"])

    def test_multicrop_deadline_reports_attempted_request_count(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop-deadline.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        clock = iter([0, 0, 0, 10])
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=[{"box": [0, 0, 80, 80], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]}]), \
                patch.object(extract.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(extract, "_run_sticky_resident_request", side_effect=TimeoutError("deadline")), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[2]["request_count"], 1)
        self.assertEqual(result[2]["failure_class"], "deadline")

    def test_multicrop_schema_failure_preserves_bounded_reason(self):
        from PIL import Image

        source = Path(self.tmp.name) / "multicrop-schema.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "_physical_note_regions", return_value=[{"box": [0, 0, 80, 80], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]}]), \
                patch.object(extract, "_run_sticky_resident_request", return_value=({"findings": [{"content": "bad"}]}, {})), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        self.assertEqual(result[2]["request_count"], 1)
        self.assertEqual(result[2]["failure_class"], "schema")
        self.assertEqual(result[2]["failure_code"], "MULTICROP_SCHEMA_INVALID")
        self.assertEqual(result[2]["response_shape"], {
            "type": "object",
            "keys": ["findings"],
            "findings": {"type": "array", "length": 1, "item_shapes": [["content"]]},
        })
        self.assertEqual(result[2]["failure_reason"], "invalid multicrop schema")

    def test_cli_flags_make_multicrop_exclusive_of_indexed_resident_route(self):
        from PIL import Image

        source = Path(self.tmp.name) / "cli-multicrop.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        response = {"notes": [{"content": "from crop", "confidence": 0.9, "languages": ["en"]}]}
        calls = []

        prompts = []

        def multicrop_request(*args):
            calls.append("multicrop")
            prompts.append(args[1])
            return response, {}

        with patch.dict(os.environ, {
            "OCR_STICKY_MULTICROP_ENABLED": "1",
            "OCR_STICKY_RESIDENT_ENABLED": "1",
        }), patch.object(extract, "_run_sticky_resident_request", side_effect=multicrop_request), \
                patch.object(extract, "STICKY_MULTICROP_MAX_REQUESTS", 1), \
                patch.object(extract, "_physical_note_regions", return_value=[
                    {"box": [10, 10, 90, 90], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]},
                ]):
            with patch.object(sys, "argv", ["extract.py", str(source), "--kind", "image", "--ocr-engine", "sticky-notes", "--no-cache"]), redirect_stdout(StringIO()) as stdout:
                extract.main()

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tool"], "qwen2.5vl-resident-multicrop")
        self.assertEqual(result["meta"]["route"], "multicrop")
        self.assertEqual(calls, ["multicrop"])
        self.assertIn("80x80 pixel crop", prompts[0])
        self.assertIn("Return content only", prompts[0])
        self.assertIn("Do not output coordinates, ids, colors", prompts[0])

    def test_sticky_note_reconciliation_uses_physical_regions_not_target_count(self):
        notes = [
            {"id": "split-a", "bbox": [0.11, 0.11, 0.29, 0.29], "color": "yellow", "content": "first", "confidence": 0.7, "languages": ["en"]},
            {"id": "split-b", "bbox": [0.12, 0.2, 0.3, 0.3], "color": "yellow", "content": "second", "confidence": 0.8, "languages": ["en"]},
            {"id": "background", "bbox": [0.7, 0.7, 0.9, 0.9], "color": "neutral", "content": "invented", "confidence": 0.99, "languages": ["en"]},
        ]
        regions = [{"box": [100, 100, 300, 300]}]
        result = extract._reconcile_sticky_notes(notes, regions, (1000, 1000))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bbox"], [0.1, 0.1, 0.3, 0.3])
        self.assertEqual(result[0]["content"], "first\nsecond")

    def test_sticky_note_reconciliation_retains_valid_unmatched_notes_when_detector_underrecalls(self):
        notes = [
            {"id": "matched", "bbox": [0.1, 0.1, 0.3, 0.3], "color": "yellow", "content": "physical", "confidence": 0.8, "languages": ["en"]},
            {"id": "unmatched", "bbox": [0.6, 0.1, 0.8, 0.3], "color": "cyan", "content": "structured", "confidence": 0.9, "languages": ["en"]},
            {"id": "background", "bbox": [0.8, 0.8, 0.9, 0.9], "color": "neutral", "content": "guess", "confidence": 0.2, "languages": ["en"]},
        ]
        regions = [{"box": [100, 100, 300, 300]}]
        result = extract._reconcile_sticky_notes(notes, regions, (1000, 1000))
        self.assertEqual([item["content"] for item in result], ["physical", "structured"])

    def test_sticky_fragment_schema_is_strict_and_normalizes_pixel_boxes(self):
        payload = {"fragments": [{"id": "f1", "bbox": [100, 200, 300, 400], "color": "blue", "content": "note", "confidence": 0.8, "languages": ["en"]}]}
        normalized = extract._validate_sticky_fragments(payload, (1000, 1000))
        self.assertEqual(normalized["fragments"][0]["bbox"], [0.1, 0.2, 0.3, 0.4])
        self.assertIsNone(extract._validate_sticky_fragments({**payload, "extra": True}, (1000, 1000)))

    def test_sticky_fragment_refinement_splits_large_detector_region_and_merges_colocated(self):
        from PIL import Image, ImageDraw

        source = Path(self.tmp.name) / "fragment-refine.png"
        image = Image.new("RGB", (1000, 500), (30, 30, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 100, 300, 300), fill=(245, 220, 70))
        draw.rectangle((500, 100, 700, 300), fill=(80, 190, 220))
        image.save(source)
        fragments = [
            {"id": "a", "bbox": [0.10, 0.20, 0.30, 0.60], "color": "yellow", "content": "a", "confidence": 0.8, "languages": ["en"]},
            {"id": "b", "bbox": [0.50, 0.20, 0.70, 0.60], "color": "cyan", "content": "b", "confidence": 0.8, "languages": ["en"]},
        ]
        regions = [{"box": [0, 0, 1000, 500], "provenance": ["contrast"]}]
        refined = extract._refine_fragment_regions(str(source), fragments, regions, (1000, 500))
        self.assertEqual(len(refined), 2)
        merged = extract._merge_colocated_regions([{"box": [100, 100, 300, 300]}, {"box": [105, 105, 295, 295]}])
        self.assertEqual(len(merged), 1)
        self.assertIn("colocation-merge", merged[0]["provenance"])

    def test_heic_uses_sips_derivative_and_preserves_source(self):
        source = Path(self.tmp.name) / "note.heic"
        source.write_bytes(b"heic source")
        output = Path(self.tmp.name) / "work" / "source.png"
        output.parent.mkdir()

        def run(command, **kwargs):
            if command[:3] == ["sips", "-g", "pixelWidth"]:
                return subprocess.CompletedProcess(command, 0, b"pixelWidth: 100\npixelHeight: 200\n", b"")
            output.write_bytes(b"png derivative")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        original = source.read_bytes()
        with patch.object(extract, "which", return_value="/usr/bin/sips"), \
                patch.object(extract.subprocess, "run", side_effect=run):
            converted, meta = extract._prepare_image_input(str(source), str(output.parent), 2)
        self.assertEqual(converted, str(output))
        self.assertTrue(meta["converted"])
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(output.exists())

    def test_heic_audit_uses_sips_dimensions_when_pillow_fails(self):
        source = Path(self.tmp.name) / "note.heic"
        source.write_bytes(b"heic source")
        with patch.object(extract, "which", return_value="/usr/bin/sips"), \
                patch.object(extract, "_sips_dimensions", return_value=(123, 456)) as dimensions:
            audit = extract._extractor_audit(str(source), "image/heic", timeout=0.25)
        self.assertEqual(audit["input_dimensions"], [123, 456])
        dimensions.assert_called_once_with(str(source), 0.25)

    def test_heic_audit_uses_actual_sips_parser_when_pillow_dimensions_are_null(self):
        source = Path(self.tmp.name) / "note.heic"
        source.write_bytes(b"heic source")
        completed = subprocess.CompletedProcess(
            ["sips"], 0, b"/tmp/note.heic\n  pixelWidth: 4000\n  pixelHeight: 3000\n", b""
        )
        with patch.object(extract, "which", return_value="/usr/bin/sips"), \
                patch.object(extract.sys, "platform", "darwin"), \
                patch.object(extract.subprocess, "run", return_value=completed):
            with patch.dict(sys.modules, {"PIL.Image": None}):
                audit = extract._extractor_audit(str(source), "image/heic", timeout=0.25)
        self.assertEqual(audit["input_dimensions"], [4000, 3000])

    def test_multicrop_rejects_nonfinite_confidence_and_geometry_fields(self):
        base = {"content": "x", "confidence": 1, "languages": []}
        for field, value in (("bbox", [0, 0, float("nan"), 1]), ("bbox", [0, 0, float("inf"), 1]), ("confidence", float("nan"))):
            diagnostics = []
            self.assertIsNone(extract._validate_sticky_multicrop({"notes": [{**base, field: value}]}, (100, 100), diagnostics))
            self.assertTrue(diagnostics)

    def test_multicrop_schema_diagnostic_contains_no_model_content(self):
        from PIL import Image
        source = Path(self.tmp.name) / "multicrop-nonfinite.png"
        Image.new("RGB", (100, 100), "yellow").save(source)
        response = {"notes": [{"content": "private text", "confidence": 1, "languages": [], "bbox": [0, 0, 1, 1]}]}
        with patch.dict(os.environ, {"OCR_STICKY_MULTICROP_ENABLED": "1"}), \
                patch.object(extract, "STICKY_MULTICROP_MAX_REQUESTS", 1), \
                patch.object(extract, "_physical_note_regions", return_value=[{"box": [10, 10, 90, 90], "color": "yellow", "provenance": ["color"], "source_size": [100, 100]}]), \
                patch.object(extract, "_run_sticky_resident_request", return_value=(response, {})), \
                patch.object(extract, "_structured_local_sticky_notes", return_value=("{\"notes\":[]}", "local", {})):
            result = extract._extract_sticky_notes(str(source), 5, {})
        evidence = result[2]["geometry_evidence"]
        self.assertEqual(evidence["crop_id"], "proposal-1")
        self.assertEqual(evidence["crop_origin"], [10, 10])
        self.assertEqual(evidence["crop_size"], [80, 80])
        self.assertEqual(evidence["source_size"], [100, 100])
        self.assertNotIn("bbox", evidence)
        self.assertNotIn("private text", json.dumps(evidence))

    def test_heic_conversion_respects_pixel_cap(self):
        source = Path(self.tmp.name) / "large.heic"
        source.write_bytes(b"heic source")
        workdir = Path(self.tmp.name) / "work"
        workdir.mkdir()
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["sips", "-g", "pixelWidth"]:
                return subprocess.CompletedProcess(command, 0, b"pixelWidth: 10000\npixelHeight: 10000\n", b"")
            Path(command[command.index("--out") + 1]).write_bytes(b"png")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(extract, "IMAGE_MAX_PIXELS", 100), \
                patch.object(extract, "which", return_value="/usr/bin/sips"), \
                patch.object(extract.subprocess, "run", side_effect=run):
            _, meta = extract._prepare_image_input(str(source), str(workdir), 2)
        self.assertTrue(meta["resampled"])
        self.assertIn("--resampleHeightWidthMax", commands[1])

    def test_document_classifier_requires_text_and_layout_signal(self):
        from PIL import Image, ImageDraw

        letter = Path(self.tmp.name) / "letter.png"
        image = Image.new("RGB", (1600, 2200), "white")
        draw = ImageDraw.Draw(image)
        draw.text((160, 180), "Recovered Letter\nReference 0001", fill="black")
        image.save(letter)
        with patch.object(extract, "which", return_value="tesseract"), patch.object(
                extract, "_run_tesseract_tsv", return_value=(0, "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t160\t180\t400\t40\t90\tRecovered\n"
                "5\t1\t1\t1\t2\t1\t160\t240\t400\t40\t90\tLetter\n", "")):
            is_document, features = extract._document_image(str(letter), 1)
        self.assertTrue(is_document)
        self.assertTrue(features["text_layout_signal"])

        card = Path(self.tmp.name) / "ui-card.png"
        Image.new("L", (1600, 900), 145).save(card)
        with patch.object(extract, "which", return_value="tesseract"), patch.object(
                extract, "_run_tesseract_tsv", return_value=(0, "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n", "")):
            is_document, features = extract._document_image(str(card), 1)
        self.assertFalse(is_document)
        self.assertFalse(features["text_layout_signal"])

    def test_granite_docling_classification_accepts_supported_label(self):
        source = Path(self.tmp.name) / "note.png"
        source.write_bytes(b"image")
        response = {"message": {"content": json.dumps({"label": "handwriting", "confidence": 0.91, "features": {"ink": True, "page": True}})}}
        with patch.object(extract, "_granite_docling_available", return_value=(True, {"model": "granite-docling:latest", "reason": "installed"})), \
                patch.object(extract, "_ollama_json", return_value=response) as call:
            result, meta = extract._granite_docling_classify(str(source), 1)
        self.assertEqual(result["label"], "handwriting")
        self.assertEqual(result["confidence"], 0.91)
        self.assertEqual(meta["engine"], "granite-docling")
        self.assertEqual(call.call_args.args[0], "http://127.0.0.1:11434/api/chat")

    def test_granite_docling_rejects_unsupported_label(self):
        source = Path(self.tmp.name) / "note.png"
        source.write_bytes(b"image")
        response = {"message": {"content": json.dumps({"label": "receipt", "confidence": 0.99, "features": {}})}}
        with patch.object(extract, "_granite_docling_available", return_value=(True, {"reason": "installed"})), \
                patch.object(extract, "_ollama_json", return_value=response):
            result, meta = extract._granite_docling_classify(str(source), 1)
        self.assertIsNone(result)
        self.assertEqual(meta["engine"], "deterministic")
        self.assertEqual(meta["granite_docling"]["reason"], "invalid-response")

    def test_granite_docling_down_falls_back_to_deterministic_classifier(self):
        with patch.object(extract, "_granite_docling_available", return_value=(False, {"reason": "ollama-unavailable"})), \
                patch.object(extract, "_document_image", return_value=(True, {"text_layout_signal": True})):
            result, meta = extract._classify_image(str(Path(self.tmp.name) / "note.png"), 1)
        self.assertEqual(result["label"], "document")
        self.assertEqual(meta["engine"], "deterministic")
        self.assertEqual(meta["granite_docling"]["reason"], "ollama-unavailable")

    def test_granite_docling_missing_tag_fails_closed(self):
        with patch.dict(os.environ, {"GRANITE_DOCLING_MODEL": "granite-docling:latest"}), \
                patch.object(extract.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"models": [{"name": "gemma4:12b"}]}).encode()
            available, meta = extract._granite_docling_available(1)
        self.assertFalse(available)
        self.assertEqual(meta["reason"], "model-not-installed")

    def test_document_classifier_uses_single_remaining_timeout(self):
        from PIL import Image

        source = Path(self.tmp.name) / "letter.png"
        Image.new("RGB", (1600, 2200), "white").save(source)
        observed = []

        def run_tsv(_path, timeout, psm=11):
            observed.append(timeout)
            return 0, "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n", ""

        with patch.object(extract, "which", return_value="tesseract"), patch.object(
                extract, "_run_tesseract_tsv", side_effect=run_tsv):
            extract._document_image(str(source), 0.25)
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 0.25)

    def test_image_ocr_uses_apple_only_when_tesseract_unavailable(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        with patch.object(extract, "_image_derivative", return_value=("/tmp/upright.png", {"angle": 0})), \
                patch.object(extract, "which", return_value=None), \
                patch.object(extract, "_run_apple_vision_ocr", return_value=("upright text", {})):
            text, tool, meta = extract.extract_image(str(source), 1)
        self.assertEqual((text, tool), ("upright text", "apple-vision"))
        self.assertEqual(meta["preprocessing"]["angle"], 0)

    def test_image_ocr_uses_apple_vision_before_vlm_after_low_tesseract_confidence(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        low_tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t1\t1\t0\t0\t1\t1\t20\ttext\n"
        with patch.object(extract, "_image_derivative", return_value=("/tmp/gray.png", {"angle": 0})), \
                patch.object(extract, "_run_tesseract_tsv", return_value=(0, low_tsv, "")), \
                patch.object(extract, "_run_tesseract", return_value=(0, "text\n", "")), \
                patch.object(extract, "_run_apple_vision_ocr", return_value=("Apple text", {"revision": 3})), \
                patch.object(extract, "_run_vlm_ocr") as vlm, \
                patch.object(extract, "which", return_value="tesseract"):
            text, tool, meta = extract.extract_image(str(source), 1)
        self.assertEqual((text, tool), ("Apple text", "apple-vision"))
        self.assertEqual(meta["apple_vision"]["revision"], 3)
        vlm.assert_not_called()

    def test_image_ocr_selects_rotation_by_bounded_confidence(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        derivatives = []

        def derivative(_path, _workdir, angle=0, threshold=False):
            derivatives.append((angle, threshold))
            return f"/tmp/{angle}-{threshold}.png", {"angle": angle, "threshold": threshold}

        tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\trotated text\n"
        low_tsv = tsv.replace("90\trotated text", "20\trotated text")
        with \
                patch.object(extract, "_image_derivative", side_effect=derivative), \
                patch.object(extract, "_run_vlm_ocr", return_value=(None, None, {})), \
                patch.object(extract, "which", return_value="tesseract"), \
                patch.object(extract, "_run_tesseract_tsv", side_effect=[(0, low_tsv, ""), (0, low_tsv, ""), (0, tsv, ""), (0, low_tsv, "")]):
            text, tool, meta = extract.extract_image(str(source), 1)
        self.assertEqual(text, "rotated text")
        self.assertEqual(tool, "tesseract")
        self.assertEqual(meta["rotation"], 180)
        self.assertEqual(meta["psm"], 11)
        self.assertEqual([angle for angle, threshold in derivatives], [0, 90, 180, 270])

    def test_ocr_text_orders_tsv_lines_by_page_coordinates(self):
        tsv = (
            "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t2\t1\t10\t200\t20\t10\t90\tsecond\n"
            "5\t1\t1\t1\t1\t1\t10\t100\t20\t10\t90\tfirst\n"
            "5\t1\t1\t1\t1\t2\t40\t100\t20\t10\t90\tline\n"
        )
        self.assertEqual(extract._ocr_text(tsv), "first line\nsecond")

    def test_image_ocr_uses_psm_fallback_in_order(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        low_tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t1\t1\t0\t0\t1\t1\t20\ttext\n"
        good_tsv = low_tsv.replace("20\ttext", "50\ttext")
        calls = []

        def run_tsv(_path, _timeout, psm=11):
            calls.append(psm)
            return 0, good_tsv if psm == 6 else low_tsv, ""

        with patch.object(extract, "_image_derivative", return_value=("/tmp/gray.png", {"angle": 0})), \
                patch.object(extract, "_run_vlm_ocr", return_value=(None, None, {})), \
                patch.object(extract, "which", return_value="tesseract"), \
                patch.object(extract, "_run_tesseract_tsv", side_effect=run_tsv):
            text, tool, meta = extract.extract_image(str(source), 1)
        self.assertEqual(calls, [11, 11, 11, 11, 3, 6])
        self.assertEqual((text, tool), ("text", "tesseract"))
        self.assertEqual(meta["psm"], 6)

    def test_image_ocr_returns_direct_text_from_selected_candidate(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        with patch.object(extract, "_image_derivative", return_value=("/tmp/upright.png", {"angle": 0})), \
                patch.object(extract, "_run_vlm_ocr", return_value=(None, None, {})), \
                patch.object(extract, "which", return_value="tesseract"), \
                patch.object(extract, "_run_tesseract_tsv", return_value=(0, "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t1\t1\t1\t1\t0\t0\t1\t1\t90\tparsed\n", "")), \
                patch.object(extract.os.path, "exists", return_value=True), \
                patch.object(extract, "_run_tesseract", return_value=(0, "direct\n", "")):
            text, tool, _ = extract.extract_image(str(source), 1)
        self.assertEqual((text, tool), ("direct", "tesseract"))

    def test_correction_failure_rolls_back_to_original(self):
        with patch.object(extract, "_correction_enabled", return_value=True), patch.object(
                extract.urllib.request, "urlopen", side_effect=ValueError("bad response")):
            corrected, meta = extract.correct_ocr_text("raw OCR", 0.01)
        self.assertEqual(corrected, "raw OCR")
        self.assertEqual(meta["status"], "abstain")

    def test_malformed_live_response_abstains_without_mutation(self):
        with patch.object(extract, "_correction_enabled", return_value=True), patch.object(
                extract.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"message": {"content": "not json"}}).encode()
            corrected, meta = extract.correct_ocr_text("Total 1O0 USD", 1)
        self.assertEqual(corrected, "Total 1O0 USD")
        self.assertEqual(meta["status"], "abstain")

    def test_valid_live_response_applies_only_validated_span(self):
        payload = {"corrections": [{"source": "1O0", "replacement": "100", "start": 6,
                                     "confidence": 0.95, "provenance": {"reason": "digit OCR"}}]}
        with patch.object(extract, "_correction_enabled", return_value=True), patch.object(
                extract.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"message": {"content": json.dumps(payload)}}).encode()
            corrected, meta = extract.correct_ocr_text("Total 1O0 USD", 1)
        self.assertEqual(corrected, "Total 100 USD")
        self.assertEqual(meta["status"], "corrected")

    def test_valid_live_response_can_omit_unreliable_start(self):
        payload = {"corrections": [{"source": "1O0", "replacement": "100",
                                     "confidence": 0.95, "provenance": {"reason": "digit OCR"}}]}
        with patch.object(extract, "_correction_enabled", return_value=True), patch.object(
                extract.urllib.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.read.return_value = json.dumps({"message": {"content": json.dumps(payload)}}).encode()
            corrected, meta = extract.correct_ocr_text("Total 1O0 USD", 1)
        self.assertEqual(corrected, "Total 100 USD")
        self.assertEqual(meta["status"], "corrected")

    def test_extractor_success_then_disk_cache(self):
        source = Path(self.tmp.name) / "sample.txt"
        source.write_text("deterministic text")
        env = os.environ.copy()
        env["TMPDIR"] = self.tmp.name
        first = subprocess.run(
            [sys.executable, str(EXTRACT_PATH), str(source), "--kind", "text", "--no-cache"],
            text=True, capture_output=True, env=env, check=True,
        )
        self.assertEqual(json.loads(first.stdout)["text"], "deterministic text")

    def test_correction_output_keeps_original_authoritative(self):
        fields = extract.correction_fields("Total 1O0 USD", "Total 100 USD")
        self.assertEqual(fields["text"], "Total 1O0 USD")
        self.assertEqual(fields["original_text"], "Total 1O0 USD")
        self.assertEqual(fields["corrected_text"], "Total 100 USD")

    def test_original_and_corrected_inline_text_share_one_cap(self):
        fields, bounds = extract.bounded_correction_fields("abcdefgh", "ABCDEFGH", 10)
        self.assertEqual(fields["original_text"], "abcdefgh")
        self.assertEqual(fields["corrected_text"], "AB")
        self.assertEqual(len(fields["original_text"]) + len(fields["corrected_text"]), 10)
        self.assertTrue(bounds["corrected_truncated"])

    def test_original_consumes_cap_before_corrected_alternative(self):
        fields, bounds = extract.bounded_correction_fields("abcdefgh", "ABCDEFGH", 4)
        self.assertEqual(fields["original_text"], "abcd")
        self.assertEqual(fields["corrected_text"], "")
        self.assertEqual(len(fields["original_text"]) + len(fields["corrected_text"]), 4)
        self.assertTrue(bounds["original_truncated"])

    def test_cli_correction_output_keeps_original_authoritative(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        output = StringIO()
        with patch.object(extract, "extract_image", return_value=("Total 1O0 USD", "tesseract", {})), \
                patch.object(extract, "correct_ocr_text", return_value=("Total 100 USD", {"status": "corrected"})), \
                patch.object(sys, "argv", [str(EXTRACT_PATH), str(source), "--kind", "image", "--ocr-correction", "--no-cache"]), \
                redirect_stdout(output):
            extract.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["text"], "Total 1O0 USD")
        self.assertEqual(result["original_text"], "Total 1O0 USD")
        self.assertEqual(result["corrected_text"], "Total 100 USD")

    def test_cli_correction_output_respects_aggregate_cap(self):
        source = Path(self.tmp.name) / "scan.png"
        source.write_bytes(b"fixture")
        output = StringIO()
        with patch.object(extract, "extract_image", return_value=("abcdefgh", "tesseract", {})), \
                patch.object(extract, "correct_ocr_text", return_value=("ABCDEFGH", {"status": "corrected"})), \
                patch.object(sys, "argv", [str(EXTRACT_PATH), str(source), "--kind", "image", "--max-chars", "10", "--ocr-correction", "--no-cache"]), \
                redirect_stdout(output):
            extract.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["inline_chars"], 10)
        self.assertEqual(len(result["original_text"]) + len(result["corrected_text"]), 10)
        self.assertEqual(result["original_text"], "abcdefgh")
        self.assertEqual(result["corrected_text"], "AB")


if __name__ == "__main__":
    unittest.main()
