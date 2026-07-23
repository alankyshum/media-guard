import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import UnidentifiedImageError


PATH = Path(__file__).with_name("run_sticky_multicrop_benchmark.py")
SPEC = importlib.util.spec_from_file_location("sticky_benchmark", PATH)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(benchmark)


class StickyMulticropBenchmarkTests(unittest.TestCase):
    def test_command_and_environment_bind_multicrop(self):
        command = benchmark.build_command(Path("extract.py"), Path("fixture.jpg"), 10)
        env = benchmark.build_env()
        self.assertIn("--ocr-engine", command)
        self.assertIn("sticky-notes", command)
        self.assertIn("--no-cache", command)
        self.assertEqual(env["OCR_STICKY_MULTICROP_ENABLED"], "1")
        self.assertEqual(env["OCR_STICKY_RESIDENT_ENABLED"], "1")
        self.assertEqual(env["OCR_VLM_BASE_URL"], benchmark.EXPECTED_BASE_URL)
        self.assertEqual(env["OCR_VLM_MODEL"], benchmark.EXPECTED_MODEL)

    def test_audit_preflight_uses_no_inference_flags(self):
        command = benchmark.build_audit_command(Path("extract.py"), Path("fixture.heic"), 10)
        self.assertIn("--audit-only", command)
        with patch.object(benchmark.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"provenance": {"audit": {"input_sha256": "f", "extractor_source_sha256": "e"}}}), stderr="")) as run:
            audit = benchmark.audit_current_source(Path("extract.py"), Path("fixture.heic"), 10)
        self.assertEqual(audit["input_sha256"], "f")
        self.assertEqual(run.call_args.kwargs["env"]["OCR_VLM_ENABLED"], "0")
        self.assertEqual(run.call_args.kwargs["env"]["GRANITE_DOCLING_ENABLED"], "0")

    def test_mock_result_accepts_sticky_multicrop_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture.heic"
            oracle = root / "oracle.json"
            extract = root / "extract.py"
            fixture.write_bytes(b"fixture")
            extract.write_bytes(b"extractor")
            oracle.write_text(json.dumps({"notes": [{"bounding_box": [0, 0, 1, 1], "text": "note"}], "image_size": [100, 100]}))
            output = root / "run"
            response = {"status": "ok", "tool": benchmark.EXPECTED_TOOL, "text": json.dumps({"notes": [{"id": "1", "bbox": [0, 0, 1, 1], "color": "yellow", "content": "note", "confidence": 0.9, "languages": ["en"]}]}), "meta": {"route": "multicrop", "request_count": 1}, "provenance": {"audit": {"input_dimensions": [100, 100], "input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor"}}}
            completed = SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=output, timeout=10, oracle_count=1, iou=0.5, min_tp=1, max_fp=0)
            with patch.object(benchmark.subprocess, "run", return_value=completed), \
                 patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "03e01oracle", "extractor"] * 2), \
                 patch.object(benchmark, "image_dimensions", return_value=[100, 100]), \
                 patch.object(benchmark, "audit_current_source", return_value={"input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor", "input_dimensions": [100, 100]}):
                code, record = benchmark.run_once(args)
            self.assertEqual(code, 0)
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(record["route"], "multicrop")
            self.assertEqual(record["tool"], "qwen2.5vl-resident-multicrop")
            self.assertTrue(record["run_id"])

    def test_stale_extractor_audit_is_rejected_before_scoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"f")
            oracle.write_text(json.dumps({"notes": []}))
            extract.write_bytes(b"e")
            response = {"status": "ok", "text": '{"notes": []}', "provenance": {"audit": {"input_dimensions": [100, 100], "input_sha256": "38ff-stale", "extractor_source_sha256": "extractor"}}}
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")), \
                    patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "03e01oracle", "extractor"] * 2), \
                 patch.object(benchmark, "image_dimensions", return_value=[100, 100]), \
                 patch.object(benchmark, "audit_current_source", return_value={"input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor", "input_dimensions": [100, 100]}), \
                 patch.object(benchmark, "score", side_effect=AssertionError("scoring must not run")):
                code, record = benchmark.run_once(args)
            self.assertNotEqual(code, 0)
            self.assertEqual(record["rejection_code"], "STALE_SOURCE_OR_ARTIFACT")

    def test_mock_wrong_route_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"f")
            extract.write_bytes(b"e")
            oracle.write_text(json.dumps({"notes": []}))
            response = {"status": "ok", "tool": "qwen2.5vl-resident-indexed", "text": '{"notes":[]}', "meta": {"route": "resident-indexed", "request_count": 2}, "provenance": {"audit": {"input_dimensions": [100, 100], "input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor"}}}
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")), \
                 patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "03e01oracle", "extractor"] * 2), \
                 patch.object(benchmark, "image_dimensions", return_value=[100, 100]), \
                 patch.object(benchmark, "audit_current_source", return_value={"input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor", "input_dimensions": [100, 100]}):
                code, record = benchmark.run_once(args)
            self.assertNotEqual(code, 0)
            self.assertEqual(record["rejection_code"], "ROUTE_MISMATCH")

    def test_fixture_identity_mutation_is_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"fixture")
            oracle.write_text(json.dumps({"notes": []}))
            extract.write_bytes(b"extract")
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark, "sha256_file", side_effect=["wrong-fixture", "03e01oracle", "extractor"]), \
                 patch.object(benchmark.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "authoritative HEIC"):
                    benchmark.run_once(args)
            run.assert_not_called()

    def test_oracle_identity_mutation_is_rejected_before_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"fixture")
            oracle.write_text(json.dumps({"notes": []}))
            extract.write_bytes(b"extract")
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "wrong-oracle", "extractor"]), \
                 patch.object(benchmark.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "authoritative benchmark oracle"):
                    benchmark.run_once(args)
            run.assert_not_called()

    def test_fixture_dimension_mutation_is_rejected_after_inference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"fixture")
            oracle.write_text(json.dumps({"notes": []}))
            extract.write_bytes(b"extract")
            response = {"status": "ok", "tool": benchmark.EXPECTED_TOOL, "text": '{"notes":[]}', "meta": {"route": "multicrop", "request_count": 1}, "provenance": {"audit": {"input_dimensions": [99, 100], "input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor"}}}
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")), \
                 patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "03e01oracle", "extractor"] * 2), \
                 patch.object(benchmark, "image_dimensions", return_value=[100, 100]), \
                 patch.object(benchmark, "audit_current_source", return_value={"input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor", "input_dimensions": [100, 100]}):
                code, record = benchmark.run_once(args)
            self.assertNotEqual(code, 0)
            self.assertEqual(record["rejection_code"], "FIXTURE_DIMENSIONS_MISMATCH")

    def test_heic_dimensions_fall_back_to_timeout_bounded_sips(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.heic"
            path.write_bytes(b"not pillow")
            completed = SimpleNamespace(returncode=0, stdout="pixelWidth: 4000\npixelHeight: 3000\n")
            with patch.object(benchmark.sys, "platform", "darwin"), \
                    patch("PIL.Image.open", side_effect=UnidentifiedImageError("cannot identify image file")), \
                    patch.object(benchmark.subprocess, "run", return_value=completed) as run:
                self.assertEqual(benchmark.image_dimensions(path), [4000, 3000])
                self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_postrun_identity_mutation_is_rejected_before_scoring(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, oracle, extract = root / "f.heic", root / "o.json", root / "e.py"
            fixture.write_bytes(b"fixture")
            oracle.write_text(json.dumps({"notes": []}))
            extract.write_bytes(b"extract")
            response = {"status": "ok", "tool": benchmark.EXPECTED_TOOL, "text": '{"notes":[]}', "meta": {"route": "multicrop", "request_count": 1}, "provenance": {"audit": {"input_dimensions": [100, 100], "input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor"}}}
            args = SimpleNamespace(fixture=fixture, oracle=oracle, extract=extract, out=root / "run", timeout=10, oracle_count=0, iou=0.5, min_tp=0, max_fp=0)
            with patch.object(benchmark.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")), \
                 patch.object(benchmark, "sha256_file", side_effect=["8c9a81fixture", "03e01oracle", "extractor", "changed-fixture", "03e01oracle", "extractor"]), \
                 patch.object(benchmark, "image_dimensions", return_value=[100, 100]), \
                 patch.object(benchmark, "audit_current_source", return_value={"input_sha256": "8c9a81fixture", "extractor_source_sha256": "extractor", "input_dimensions": [100, 100]}), \
                 patch.object(benchmark, "score", side_effect=AssertionError("scoring must not run after identity drift")):
                code, record = benchmark.run_once(args)
            self.assertNotEqual(code, 0)
            self.assertEqual(record["rejection_code"], "IDENTITY_DRIFT")


if __name__ == "__main__":
    unittest.main()
