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

        with patch.object(extract, "which", return_value="tesseract"), patch.object(
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
