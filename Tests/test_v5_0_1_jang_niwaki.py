"""v5.0.1: JANG (jangtq2 / JANGH2) detection and Niwaki loaderId regression.

JANG artifacts (``jang_config.json`` with ``format == "jangtq2"`` /
``weight_format`` containing ``jangtq``, a ``config.json`` quantization
``mode == "jangtq2"``, or an ``osaurus.json`` marker) cannot run on any
runtime MLXBar can start, so they are classified with
``loaderId == "jang-osaurus-required"`` and every runtime ``unsupported``,
and the worker preflight refuses them with ``MODEL_JANG_OSAURUS_REQUIRED``
before any stock load. The Niwaki ``niwaki-qwen4-exp`` loaderId (v3.3.0)
must survive alongside it: a plain affine ``qwen3_5_moe`` has no loaderId,
while a ``qwen4_exp`` artifact with a ``niwaki`` config block does.

Everything runs on fake model directories in a temp dir -- no real
models, no weights, no runtimes. Fixture shape follows
``Tests/test_v3_3_0_loader.py`` (``config.json`` + ``tokenizer.json`` +
a minimal ``model.safetensors`` stub + index).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))
sys.path.insert(0, str(Path(__file__).parents[1] / "Coordinator"))

from common.jang import is_jang_artifact  # noqa: E402
from common.model_errors import ModelLoadError  # noqa: E402
from mlx_vlm_worker import loaders  # noqa: E402
from mlxbar.catalog.classifier import classify  # noqa: E402

JANG_LOADER_ID = "jang-osaurus-required"
NIWAKI_LOADER_ID = "niwaki-qwen4-exp"

ALL_UNSUPPORTED = {"mlx-lm": "unsupported", "mlx-vlm": "unsupported",
                   "omlx": "unsupported", "lm-studio": "unsupported"}

# A normal affine-quantized qwen3_5_moe: bits without any JANG marker.
AFFINE_CONFIG = {
    "model_type": "qwen3_5_moe",
    "architectures": ["Qwen3_5MoeForConditionalGeneration"],
    "quantization": {"mode": "affine", "bits": 4},
}
NIWAKI_CONFIG = {
    "model_type": "qwen4_exp",
    "architectures": ["Qwen4ExpForConditionalGeneration"],
    "vision_config": {"depth": 27},
    "language_model_only": False,
    "niwaki": {"shared_only_layers": [1], "kept_experts": {"0": [1, 2]},
               "kept_routing": {}},
    "quantization": {"bits": 3},
}


def _write_base(path: Path, config: dict) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 16)
    (path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"language_model.layers.0.weight":
                                   "model.safetensors"}}), encoding="utf-8")
    return path


def make_jang(root: Path, name: str = "jang") -> Path:
    """JANG via ``jang_config.json`` (format=jangtq2) + quantization mode."""
    path = _write_base(root / name, dict(
        AFFINE_CONFIG, quantization={"mode": "jangtq2", "bits": 2}))
    (path / "jang_config.json").write_text(
        json.dumps({"format": "jangtq2"}), encoding="utf-8")
    return path


def make_jang_osaurus(root: Path, name: str = "jang-osaurus") -> Path:
    """JANG via ``osaurus.json`` alone (no other JANG evidence)."""
    path = _write_base(root / name, dict(AFFINE_CONFIG))
    (path / "osaurus.json").write_text("{}", encoding="utf-8")
    return path


def make_affine(root: Path, name: str = "affine") -> Path:
    return _write_base(root / name, dict(AFFINE_CONFIG))


def make_niwaki(root: Path, name: str = "niwaki") -> Path:
    return _write_base(root / name, dict(NIWAKI_CONFIG))


class ClassifierJangTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_jang_config_and_mode_are_reported_as_unsupported(self):
        result = classify(make_jang(self.root))
        self.assertEqual(result["loaderId"], JANG_LOADER_ID)
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["runtimeFit"], ALL_UNSUPPORTED)
        self.assertIn("jangtq2", result["reason"])

    def test_osaurus_json_alone_is_reported_as_unsupported(self):
        result = classify(make_jang_osaurus(self.root))
        self.assertEqual(result["loaderId"], JANG_LOADER_ID)
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["runtimeFit"], ALL_UNSUPPORTED)

    def test_normal_affine_model_needs_no_bundled_loader(self):
        result = classify(make_affine(self.root))
        self.assertIsNone(result["loaderId"])

    def test_niwaki_keeps_its_loader_id(self):
        result = classify(make_niwaki(self.root))
        self.assertEqual(result["loaderId"], NIWAKI_LOADER_ID)


class LoaderPreflightJangTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_generic_preflight_rejects_jang(self):
        with self.assertRaises(ModelLoadError) as raised:
            loaders.generic_preflight(make_jang(self.root))
        self.assertEqual(raised.exception.code, "MODEL_JANG_OSAURUS_REQUIRED")
        self.assertIn("jangtq2", raised.exception.detail or "")

    def test_plan_rejects_jang_regardless_of_bundled_loaders(self):
        path = make_jang(self.root)
        for bundled in (False, True):
            with self.subTest(bundled_loaders_enabled=bundled):
                with self.assertRaises(ModelLoadError) as raised:
                    loaders.plan(path, bundled_loaders_enabled=bundled)
                self.assertEqual(raised.exception.code, "MODEL_JANG_OSAURUS_REQUIRED")
                self.assertIn("jangtq2", raised.exception.detail or "")


class LoaderPlanNiwakiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_plan_without_bundled_loaders_refuses(self):
        with self.assertRaises(ModelLoadError) as raised:
            loaders.plan(make_niwaki(self.root), bundled_loaders_enabled=False)
        self.assertEqual(raised.exception.code, "MODEL_NEEDS_BUNDLED_LOADER")

    def test_plan_with_bundled_loaders_routes_to_niwaki(self):
        # plan only: load_impl is never reached.
        report = loaders.plan(make_niwaki(self.root), bundled_loaders_enabled=True)
        self.assertEqual(report["loaderId"], NIWAKI_LOADER_ID)
        self.assertIsNotNone(report["loader"])


class JangArtifactPredicateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_normal_affine_model_is_not_jang(self):
        path = make_affine(self.root)
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        self.assertFalse(is_jang_artifact(config, path))

    def test_jang_model_is_jang(self):
        path = make_jang(self.root)
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(is_jang_artifact(config, path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
