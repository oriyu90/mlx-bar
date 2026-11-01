"""v4.0.1 (WP-7): mlx-vlm 0.7.11's new vision/OCR `model_type`s.

mlx-vlm 0.7.11 ships vision/OCR families whose ``model_type`` no
``ARCH_VISION_TOKENS`` entry or ``ARCH_VISION_MARKERS`` substring covers
(``minicpm_ocr``, ``minicpm_ocr_jev``, ``nemotron_parse_v3_5``). The
``*_omni`` LFM3 exports are vision+audio models with no mlx-lm
implementation, so they must never be routed to mlx-lm either.

The fix is one exact-identity check in ``_vlm_architectures`` fed by
``MLX_VLM_VISION_MODEL_TYPES``; these tests pin both the new positives and
the lookalikes that must stay text-only (``lfm3``, ``oxr``) -- an exact
casefolded comparison is the only reason ``lfm3`` cannot match ``lfm3_vl``.

Everything runs on fake model directories: no mlx, no network, no weights.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mlxbar.catalog import classifier
from mlxbar.catalog.classifier import classify, detect_engine

# Text-only families whose names sit awkwardly close to the new vision set.
TEXT_ONLY_MODEL_TYPES = ("lfm3", "oxr")
# Already matched before this patch through the "vl" token; kept as guards.
PRE_EXISTING_VISION_MODEL_TYPES = ("lfm3_vl", "d1_lfm3_vl")
CONTROL_TEXT_MODEL_TYPE = "llama"


def make_model_dir(root: Path, name: str, model_type: str) -> Path:
    """A config-only model dir that ``classify`` accepts as complete.

    ``classify`` requires config.json + a ``*.safetensors`` file + a tokenizer
    file, but the fake weights are unreadable on purpose so the tensor
    inventory stays ``known=False`` and vision is decided from config evidence
    alone (the historical behaviour).
    """
    path = root / name
    path.mkdir(parents=True)
    (path / "config.json").write_text(
        json.dumps({"model_type": model_type}), encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 8)
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    return path


class VisionModelTypeSetTests(unittest.TestCase):
    """The set is the contract; keep its membership no wider than evidence."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_every_member_classifies_as_vision_on_mlx_vlm(self):
        for model_type in sorted(classifier.MLX_VLM_VISION_MODEL_TYPES):
            with self.subTest(model_type=model_type):
                result = classify(make_model_dir(self.root, model_type, model_type))
                self.assertIs(result["capabilities"]["vision"], True)
                self.assertEqual(result["engine"], "mlx-vlm")
                self.assertEqual(result["format"], "mlx_vlm")
                self.assertEqual(result["modalities"], ["text", "image"])
                self.assertEqual(result["runtimeFit"]["mlx-lm"], "unsupported")
                self.assertEqual(result["evidence"]["engineBranch"], "vlm_arch")

    def test_text_only_lookalikes_are_not_members(self):
        for model_type in TEXT_ONLY_MODEL_TYPES:
            with self.subTest(model_type=model_type):
                self.assertNotIn(model_type, classifier.MLX_VLM_VISION_MODEL_TYPES)

    def test_text_only_lookalikes_classify_as_text(self):
        for model_type in TEXT_ONLY_MODEL_TYPES:
            with self.subTest(model_type=model_type):
                result = classify(make_model_dir(self.root, model_type, model_type))
                self.assertIs(result["capabilities"]["vision"], False)
                self.assertEqual(result["engine"], "mlx-lm")
                self.assertEqual(result["modalities"], ["text"])
                self.assertNotEqual(result["evidence"]["engineBranch"], "vlm_arch")

    def test_pre_existing_vision_families_still_match(self):
        """`lfm3_vl`/`d1_lfm3_vl` matched before the exact-identity check was
        added (the "vl" token); the new branch must not narrow them."""
        for model_type in PRE_EXISTING_VISION_MODEL_TYPES:
            with self.subTest(model_type=model_type):
                result = classify(make_model_dir(self.root, model_type, model_type))
                self.assertIs(result["capabilities"]["vision"], True)
                self.assertEqual(result["engine"], "mlx-vlm")
                self.assertEqual(result["runtimeFit"]["mlx-lm"], "unsupported")

    def test_control_text_architecture_is_untouched(self):
        result = classify(make_model_dir(
            self.root, "control", CONTROL_TEXT_MODEL_TYPE))
        self.assertIs(result["capabilities"]["vision"], False)
        self.assertEqual(result["engine"], "mlx-lm")
        self.assertEqual(result["runtimeFit"]["mlx-lm"], "supported")
        self.assertEqual(result["runtimeFit"]["mlx-vlm"], "unknown")

    def test_exact_identity_never_matches_by_substring(self):
        """A near-miss that is not a set member and has no "vl" token must not
        be pulled in -- the guard that keeps `lfm3` text-only."""
        for model_type in ("lfm3vl", "lfm3_omni_extra", "nemotron_parse"):
            with self.subTest(model_type=model_type):
                result = classify(make_model_dir(self.root, model_type, model_type))
                self.assertIs(result["capabilities"]["vision"], False)
                self.assertNotEqual(result["engine"], "mlx-vlm")

    def test_detect_engine_agrees_with_classify(self):
        """The load-time re-derivation shares `_engine_verdict`: a stale
        catalog row for a new vision family must self-heal at load time."""
        for model_type in sorted(classifier.MLX_VLM_VISION_MODEL_TYPES):
            with self.subTest(model_type=model_type):
                path = make_model_dir(self.root, f"d-{model_type}", model_type)
                self.assertEqual(detect_engine(path), "mlx-vlm")
        for model_type in TEXT_ONLY_MODEL_TYPES:
            with self.subTest(model_type=model_type):
                path = make_model_dir(self.root, f"d-{model_type}", model_type)
                self.assertEqual(detect_engine(path), "mlx-lm")


if __name__ == "__main__":
    unittest.main()
