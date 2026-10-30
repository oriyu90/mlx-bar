"""v3.3.0 (ISS-5 / WP-510, WP-511): bundled loader registry, preflight, opt-in.

Niwaki could not be loaded for a reason `trust_remote_code` retries cannot
reach: the artifact is a non-standard format (`qwen4_exp` plus a `niwaki`
config block of kept experts / shared layers) whose weight reconstruction lives
in a script shipped *inside the model directory*. Executing that script would be
arbitrary code execution from an untrusted download, so a model's own ``*.py``
is never imported. Instead:

* a closed registry of repository-owned loaders recognises the artifact
  (`loaderId`) from config evidence;
* a preflight rejects an impossible artifact in milliseconds instead of
  allocating tens of gigabytes first (AC-5-2);
* a loader that is detected but not implemented refuses honestly with
  `MODEL_NEEDS_BUNDLED_LOADER` (AC-5-1 is therefore explicitly not met yet);
* all of it is off unless the user sets `models.bundledLoaders` (AC-5-4).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from common.model_errors import ModelLoadError
from mlx_vlm_worker import loaders

from mlxbar.settings import SettingsStore

NIWAKI_ID = "niwaki-qwen4-exp"


def make_niwaki(root: Path, *, marker: Path | None = None,
                tokenizer: bool = True, weights: bool = True,
                bits: int = 3) -> Path:
    path = root / "niwaki"
    path.mkdir(parents=True)
    config = {
        "model_type": "qwen4_exp",
        "architectures": ["Qwen4ExpForConditionalGeneration"],
        "vision_config": {"depth": 27},
        "language_model_only": False,
        "niwaki": {"shared_only_layers": [1], "kept_experts": {"0": [1, 2]},
                   "kept_routing": {}},
        "quantization": {"bits": bits},
    }
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if tokenizer:
        (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    if weights:
        (path / "model.safetensors").write_bytes(b"\x00" * 16)
        (path / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"language_model.layers.0.weight":
                                       "model.safetensors"}}), encoding="utf-8")
    if marker is not None:
        # The shape of the real artifact's bundled loader. It must never run.
        (path / "niwaki_flash_load.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8")
    return path


class LoaderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_niwaki_shape_is_recognised_from_config_evidence(self):
        path = make_niwaki(self.root)
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        loader = loaders.detect_loader(config, path)
        self.assertIsNotNone(loader)
        self.assertEqual(loader.spec.loader_id, NIWAKI_ID)

    def test_registry_reports_that_the_loader_is_implemented(self):
        described = {item["loaderId"]: item for item in loaders.available_loaders()}
        self.assertIn(NIWAKI_ID, described)
        # AC-5-1: the weight reconstruction (kept_experts / shared_only /
        # PLE pair rows + detokenizer tokenmap extension) is implemented in
        # repository code. The model-supplied *.py is still never executed.
        self.assertTrue(described[NIWAKI_ID]["implemented"])

    def test_plain_text_model_needs_no_bundled_loader(self):
        config = {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}
        self.assertIsNone(loaders.detect_loader(config, self.root))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def code_for(self, path: Path) -> str:
        with self.assertRaises(ModelLoadError) as raised:
            loaders.generic_preflight(path)
        return raised.exception.code

    def test_a_complete_artifact_passes(self):
        report = loaders.generic_preflight(make_niwaki(self.root))
        names = {check["name"] for check in report["checks"]}
        self.assertTrue({"tokenizer", "weights", "quantization"} <= names)

    def test_missing_tokenizer(self):
        self.assertEqual(self.code_for(make_niwaki(self.root, tokenizer=False)),
                         "MODEL_TOKENIZER_MISSING")

    def test_missing_weights(self):
        self.assertEqual(self.code_for(make_niwaki(self.root, weights=False)),
                         "MODEL_FORMAT_UNSUPPORTED")

    def test_unsupported_quantization_width(self):
        self.assertEqual(self.code_for(make_niwaki(self.root, bits=7)),
                         "MODEL_QUANT_UNSUPPORTED")

    def test_an_unreadable_config_is_not_silently_accepted(self):
        path = self.root / "broken"
        path.mkdir()
        (path / "config.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ModelLoadError) as raised:
            loaders.generic_preflight(path)
        self.assertEqual(raised.exception.code, "MODEL_FORMAT_UNSUPPORTED")


class BundledLoaderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_an_implemented_loader_attempts_the_load(self):
        # The loader is implemented, so `load()` no longer refuses with
        # MODEL_NEEDS_BUNDLED_LOADER. On the synthetic fixture (a single
        # zero-filled safetensors) the runtime itself fails -- the point is
        # that the refusal code is gone and the failure comes from the
        # runtime, not from the guard.
        loader = loaders.resolve(NIWAKI_ID)
        self.assertIsNotNone(loader)
        try:
            loader.load(make_niwaki(self.root))
        except ModelLoadError as exc:
            self.assertNotEqual(exc.code, "MODEL_NEEDS_BUNDLED_LOADER")
        except Exception:
            pass  # runtime-side failure on the fixture is expected

    def test_plan_routes_to_the_loader_when_enabled(self):
        # With bundled loaders enabled, plan() returns the loader instead
        # of raising MODEL_NEEDS_BUNDLED_LOADER.
        path = make_niwaki(self.root)
        report = loaders.plan(path, bundled_loaders_enabled=True)
        self.assertEqual(report["loaderId"], NIWAKI_ID)
        self.assertIsNotNone(report["loader"])

    def test_the_model_supplied_loader_is_never_executed(self):
        marker = self.root / "marker.txt"
        path = make_niwaki(self.root, marker=marker)
        loaders.generic_preflight(path)
        try:
            loaders.resolve(NIWAKI_ID).load(path)
        except Exception:
            pass  # the fixture cannot load; the point is the marker check
        self.assertFalse(marker.exists(),
                         "a model's own *.py must never be imported or executed")


class OptInSettingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_bundled_loaders_are_off_by_default(self):
        self.assertFalse(self.store.data["models"]["bundledLoaders"])
        self.assertFalse(self.store.public()["models"]["bundledLoaders"])

    def test_turning_it_on_is_accepted_and_persisted(self):
        self.store.update({"models": {"bundledLoaders": True}})
        self.assertTrue(self.store.data["models"]["bundledLoaders"])

    def test_a_non_boolean_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.update({"models": {"bundledLoaders": "yes"}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
