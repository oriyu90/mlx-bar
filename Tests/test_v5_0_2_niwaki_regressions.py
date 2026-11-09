"""v5.0.2: Niwaki bundled-loader regressions (adapter / fingerprint / admission).

The bundled loader path (``niwaki-qwen4-exp``) needs four worker/coordinator
behaviours that must stay pinned:

* **Batch-invariant decode is disabled for bundled models.** mlx-vlm 0.7.12
  calls ``_batch_invariant_decode`` from ``LanguageModel.__call__`` for
  single-token decodes, bypassing the bundled loader's ``CompactMasked``
  routing. After a bundled load, ``language_model``
  ``_supports_batch_invariant_decode`` is replaced with a callable returning
  ``False`` so decode falls back to the normal forward. The helper is
  :meth:`mlx_vlm_worker.adapter.MLXVLMAdapter._disable_batch_invariant_for_bundled`
  (no-arg instance method operating on ``self.model`` /
  ``self._bundled_loader_id``) and is wired in ``load()``'s bundled branch.
  Non-bundled models keep the batch-invariant path.

* **Modalities come from :func:`common.vision.inspect`.** Niwaki declares a
  ``vision_config`` but ships 0 vision tensors (0/3434 in the real export),
  so a config-only check mislabels it ``["text", "image"]``. The adapter now
  derives ``modalities`` from ``vision.inspect(path)`` (tensor evidence
  required) with the config-only reading only as a fallback if inspect
  fails, matching the coordinator classifier's ``_capabilities``.

* **Cache fingerprint includes the loader.** :meth:`MLXVLMAdapter._model_fingerprint`
  mixes in ``loader_id`` (``_model_fingerprint(path, runtime_version,
  loader_id=None)``) so a bundled model's KV/disk cache namespace never
  collides with the stock loader's for the same directory.

* **Bundled models force the strict admission estimate.** In
  :meth:`Coordinator/mlxbar/workers/model_pool.ModelPoolSupervisor._cold_estimate`,
  a model carrying ``loaderId`` (``loaderId`` / ``loader_id``) always uses
  the ``strict`` factor (1.35x) even when ``models.pool.memoryAdmission``
  is ``balanced``.

Everything runs on fake model directories / fake workers -- no real
weights, no runtime loads. Fixture shape follows
``Tests/test_v5_0_1_jang_niwaki.py`` (``config.json`` + ``tokenizer.json``
+ minimal safetensors + index). The catalog classifier version is pinned
at ``"4"`` (v5.0.1 bump; v5.0.2 must not raise it).
"""
from __future__ import annotations

import argparse
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))
sys.path.insert(0, str(Path(__file__).parents[1] / "Coordinator"))

from common.vision import inspect as vision_inspect  # noqa: E402
from mlx_vlm_worker.adapter import MLXVLMAdapter  # noqa: E402
from mlxbar.catalog.classifier import classify  # noqa: E402
from mlxbar.cli import execute, nested_patch  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar import state as state_module  # noqa: E402
from mlxbar.workers.model_pool import ANONYMOUS_FLOOR_BYTES, GIB, ModelPoolSupervisor  # noqa: E402

NIWAKI_LOADER_ID = "niwaki-qwen4-exp"
JANG_LOADER_ID = "jang-osaurus-required"

ADAPTER_PATH = Path(__file__).parents[1] / "Workers" / "mlx_vlm_worker" / "adapter.py"


def adapter_source() -> str:
    return ADAPTER_PATH.read_text(encoding="utf-8")


NIWAKI_CONFIG = {
    "model_type": "qwen4_exp",
    "architectures": ["Qwen4ExpForConditionalGeneration"],
    "vision_config": {"depth": 27},
    "language_model_only": False,
    "niwaki": {"shared_only_layers": [1], "kept_experts": {"0": [1, 2]},
               "kept_routing": {}},
    "quantization": {"bits": 3},
}


def _write_base(path: Path, config: dict, weight_map: dict | None = None) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 16)
    if weight_map is None:
        weight_map = {"language_model.layers.0.weight": "model.safetensors"}
    (path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": weight_map}), encoding="utf-8")
    return path


def make_niwaki_text_only(root: Path, name: str = "niwaki") -> Path:
    """Niwaki synthesis: vision_config present, 0 vision tensors."""
    return _write_base(root / name, dict(NIWAKI_CONFIG))


def make_vision_model(root: Path, name: str = "vlm") -> Path:
    """Positive control: vision_config + real vision tensors (vision=True)."""
    weight_map = {
        "language_model.layers.0.weight": "model.safetensors",
        "vision_tower.layers.0.weight": "model.safetensors",
    }
    return _write_base(root / name, dict(NIWAKI_CONFIG), weight_map=weight_map)


def _modalities_from_inspect(path: Path) -> list[str]:
    return ["text", "image"] if bool(vision_inspect(path).get("vision")) else ["text"]


class _FakeLanguageModel:
    def _supports_batch_invariant_decode(self):
        return True


class _FakeModel:
    def __init__(self):
        self.language_model = _FakeLanguageModel()


class _FakeModelNoLanguageModel:
    pass


class _FakeWorker:
    """Minimal SingleWorkerSupervisor stand-in (pool construction only)."""

    instances: list = []

    def __init__(self, root, settings, **kwargs):
        self.root = root
        self.settings = settings
        self.loaded = None
        self.loading = None
        self.active_requests = {}
        self.queued_requests = {}
        self.maintenance_engines = set()
        self.memory_limit_bytes = kwargs.get("memory_limit_bytes", 0)
        self.process = None
        self.socket_path = None
        self.engine = None
        _FakeWorker.instances.append(self)

    @staticmethod
    def _is_our_worker(pid):  # noqa: ARG004
        return False

    @staticmethod
    def _process_alive(pid):  # noqa: ARG004
        return False

    async def load(self, model, engine=None, explicit=None):  # noqa: ARG002
        self.loaded = {**model, "engine": engine or model.get("engine")}
        self.engine = self.loaded["engine"]
        return self.loaded

    async def unload(self):
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    def status(self):
        return {"loadedModel": self.loaded, "loadingModel": self.loading,
                "activeRequestCount": 0, "queuedRequestCount": 0}


def _response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


# ---------------------------------------------------------------------------
# 1. Batch-invariant decode disabled for bundled models
# ---------------------------------------------------------------------------

class BatchInvariantDisabledTests(unittest.TestCase):
    def test_adapter_source_references_batch_invariant_symbols(self):
        source = adapter_source()
        self.assertIn("_supports_batch_invariant_decode", source)
        self.assertIn("_disable_batch_invariant", source)

    def test_helper_exists_and_disables_batch_invariant(self):
        adapter = MLXVLMAdapter()
        self.assertTrue(hasattr(adapter, "_disable_batch_invariant_for_bundled"),
                        "adapter must expose _disable_batch_invariant_for_bundled")
        fake = _FakeModel()
        # Precondition: batch-invariant path is on.
        self.assertIs(fake.language_model._supports_batch_invariant_decode(), True)
        adapter._bundled_loader_id = NIWAKI_LOADER_ID
        adapter.model = fake  # type: ignore[assignment]
        adapter._disable_batch_invariant_for_bundled()
        attr = fake.language_model._supports_batch_invariant_decode  # type: ignore[attr-defined]
        self.assertTrue(callable(attr), "patched attribute must be callable")
        self.assertIs(attr(), False)

    def test_helper_is_noop_for_non_bundled_models(self):
        adapter = MLXVLMAdapter()
        fake = _FakeModel()
        adapter._bundled_loader_id = None
        adapter.model = fake  # type: ignore[assignment]
        adapter._disable_batch_invariant_for_bundled()
        self.assertIs(fake.language_model._supports_batch_invariant_decode(), True)

    def test_helper_tolerates_missing_language_model(self):
        adapter = MLXVLMAdapter()
        adapter._bundled_loader_id = NIWAKI_LOADER_ID
        adapter.model = _FakeModelNoLanguageModel()  # type: ignore[assignment]
        # Must not raise: missing language_model only logs.
        adapter._disable_batch_invariant_for_bundled()

    def test_helper_creates_callable_when_missing(self):
        class NoAttrLM:
            pass

        class ModelWithBareLM:
            def __init__(self):
                self.language_model = NoAttrLM()

        adapter = MLXVLMAdapter()
        fake = ModelWithBareLM()
        self.assertFalse(hasattr(fake.language_model, "_supports_batch_invariant_decode"))
        adapter._bundled_loader_id = NIWAKI_LOADER_ID
        adapter.model = fake  # type: ignore[assignment]
        adapter._disable_batch_invariant_for_bundled()
        attr = getattr(fake.language_model, "_supports_batch_invariant_decode", None)
        self.assertTrue(callable(attr))
        self.assertIs(attr(), False)

    def test_load_wires_helper_after_bundled_load(self):
        load_source = inspect.getsource(MLXVLMAdapter.load)
        self.assertIn("_disable_batch_invariant_for_bundled", load_source)
        # The call sits in the bundled branch (after loader.load), not the
        # stock branch.
        self.assertIn("load_plan[\"loader\"]", load_source)


# ---------------------------------------------------------------------------
# 2. Modalities from vision.inspect (Niwaki: 0 vision tensors -> ["text"])
# ---------------------------------------------------------------------------

class ModalitiesVisionInspectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_niwaki_vision_inspect_is_text_only(self):
        path = make_niwaki_text_only(self.root)
        result = vision_inspect(path)
        self.assertTrue(result["readable"])
        self.assertEqual(result["tensors"]["vision"], 0)
        self.assertFalse(result["vision"])
        self.assertTrue(result["text"])

    def test_niwaki_classifier_modalities_text_only(self):
        result = classify(make_niwaki_text_only(self.root))
        self.assertEqual(result["modalities"], ["text"])
        self.assertFalse(result["capabilities"]["vision"])
        self.assertTrue(result["capabilities"]["text"])
        self.assertEqual(result["loaderId"], NIWAKI_LOADER_ID)
        # Still served by mlx-vlm (architecture requires it), just text-only.
        self.assertEqual(result["engine"], "mlx-vlm")

    def test_inspect_and_classifier_agree_on_text_only_modalities(self):
        path = make_niwaki_text_only(self.root)
        from_inspect = _modalities_from_inspect(path)
        from_classifier = classify(path)["modalities"]
        self.assertEqual(from_inspect, ["text"])
        self.assertEqual(from_classifier, ["text"])
        self.assertEqual(from_inspect, from_classifier)

    def test_vision_tensors_still_claim_image(self):
        # Positive control: the inspect rule must not collapse every
        # vision-config model to text-only.
        path = make_vision_model(self.root)
        result = vision_inspect(path)
        self.assertGreater(result["tensors"]["vision"], 0)
        self.assertTrue(result["vision"])
        self.assertEqual(_modalities_from_inspect(path), ["text", "image"])
        self.assertEqual(classify(path)["modalities"], ["text", "image"])
        self.assertTrue(classify(path)["capabilities"]["vision"])

    def test_adapter_uses_vision_inspect_for_modalities(self):
        source = adapter_source()
        # Adapter imports the shared worker-side check and consults it.
        self.assertIn("from common import vision", source)
        self.assertIn("vision.inspect", source)
        load_source = inspect.getsource(MLXVLMAdapter.load)
        self.assertIn("vision.inspect", load_source)
        self.assertIn("modalities", load_source)


# ---------------------------------------------------------------------------
# 3. Fingerprint includes loaderId
# ---------------------------------------------------------------------------

class FingerprintLoaderIdTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model_dir = _write_base(
            self.root / "model", {"model_type": "qwen3_5_moe"})

    def tearDown(self):
        self.temporary.cleanup()

    def test_fingerprint_differs_with_loader_id(self):
        plain = MLXVLMAdapter._model_fingerprint(self.model_dir, "0.6.15")
        bundled = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=NIWAKI_LOADER_ID)
        self.assertEqual(len(plain), 64)
        self.assertEqual(len(bundled), 64)
        self.assertNotEqual(plain, bundled)

    def test_fingerprint_loader_id_is_deterministic(self):
        first = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=NIWAKI_LOADER_ID)
        second = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=NIWAKI_LOADER_ID)
        self.assertEqual(first, second)

    def test_different_loaders_produce_different_fingerprints(self):
        niwaki = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=NIWAKI_LOADER_ID)
        jang = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=JANG_LOADER_ID)
        self.assertNotEqual(niwaki, jang)

    def test_default_loader_and_explicit_none_agree(self):
        implicit = MLXVLMAdapter._model_fingerprint(self.model_dir, "0.6.15")
        explicit_none = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=None)
        self.assertEqual(implicit, explicit_none)

    def test_runtime_still_changes_fingerprint(self):
        # Loader addition must not drop the existing runtime sensitivity
        # (pinned by test_worker_server).
        first = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.15", loader_id=NIWAKI_LOADER_ID)
        second = MLXVLMAdapter._model_fingerprint(
            self.model_dir, "0.6.16", loader_id=NIWAKI_LOADER_ID)
        self.assertNotEqual(first, second)


# ---------------------------------------------------------------------------
# 4. Admission: bundled models force strict estimate
# ---------------------------------------------------------------------------

class AdmissionStrictForBundledTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 0, "defaultPerModelMaxGB": 32,
            "idleTTLSeconds": 30,
        })
        _FakeWorker.instances = []
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", _FakeWorker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.pool = ModelPoolSupervisor(self.root, self.settings)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _model(model_id: str, size_gb: float = 10, **extra) -> dict:
        model = {"id": model_id, "name": model_id, "engine": "mlx-vlm",
                 "size_bytes": int(size_gb * GIB)}
        model.update(extra)
        return model

    def test_bundled_forces_strict_when_balanced(self):
        self.settings.data["models"]["pool"]["memoryAdmission"] = "balanced"
        plain = self._model("plain", 10)
        bundled = self._model("niwaki", 10, loaderId=NIWAKI_LOADER_ID)
        balanced = self.pool._cold_estimate(plain)
        strict = self.pool._cold_estimate(bundled)
        # Balanced uses 1.15x; bundled must use strict 1.35x.
        self.assertEqual(balanced, int(10 * GIB * 1.15) + ANONYMOUS_FLOOR_BYTES)
        self.assertEqual(strict, int(10 * GIB * 1.35) + ANONYMOUS_FLOOR_BYTES)
        self.assertGreater(strict, balanced)

    def test_snake_case_loader_id_also_forces_strict(self):
        self.settings.data["models"]["pool"]["memoryAdmission"] = "balanced"
        bundled = self._model("niwaki-snake", 10, loader_id=NIWAKI_LOADER_ID)
        estimate = self.pool._cold_estimate(bundled)
        self.assertEqual(estimate, int(10 * GIB * 1.35) + ANONYMOUS_FLOOR_BYTES)

    def test_non_bundled_follows_balanced_setting(self):
        self.settings.data["models"]["pool"]["memoryAdmission"] = "balanced"
        estimate = self.pool._cold_estimate(self._model("plain", 10))
        self.assertEqual(estimate, int(10 * GIB * 1.15) + ANONYMOUS_FLOOR_BYTES)

    def test_strict_setting_unaffected_by_loader(self):
        self.settings.data["models"]["pool"]["memoryAdmission"] = "strict"
        plain = self.pool._cold_estimate(self._model("plain", 10))
        bundled = self.pool._cold_estimate(
            self._model("niwaki", 10, loaderId=NIWAKI_LOADER_ID))
        expected = int(10 * GIB * 1.35) + ANONYMOUS_FLOOR_BYTES
        self.assertEqual(plain, expected)
        self.assertEqual(bundled, expected)

    def test_none_loader_id_uses_setting(self):
        self.settings.data["models"]["pool"]["memoryAdmission"] = "balanced"
        estimate = self.pool._cold_estimate(
            self._model("plain", 10, loaderId=None))
        self.assertEqual(estimate, int(10 * GIB * 1.15) + ANONYMOUS_FLOOR_BYTES)


# ---------------------------------------------------------------------------
# 5. bundledLoaders CLI / PUT / GUI same-shape parity
# ---------------------------------------------------------------------------

class BundledLoadersParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = SettingsStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_is_false(self):
        self.assertFalse(self.store.data["models"]["bundledLoaders"])
        self.assertFalse(self.store.public()["models"]["bundledLoaders"])

    def test_update_true_false_accepted_and_persisted(self):
        public = self.store.update({"models": {"bundledLoaders": True}})
        self.assertTrue(public["models"]["bundledLoaders"])
        self.assertTrue(self.store.data["models"]["bundledLoaders"])
        public = self.store.update({"models": {"bundledLoaders": False}})
        self.assertFalse(public["models"]["bundledLoaders"])

    def test_non_boolean_rejected(self):
        for bad in ("yes", "true", 1, 0, None, [], {}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.store.update({"models": {"bundledLoaders": bad}})

    def test_nested_patch_shape_matches_put_gui(self):
        # GUI: model.setConfig("models.bundledLoaders", value) PUTs the
        # nested object; CLI generic `config set` builds the same object
        # via nested_patch. Pin both to the identical shape.
        self.assertEqual(nested_patch("models.bundledLoaders", "true"),
                         {"models": {"bundledLoaders": True}})
        self.assertEqual(nested_patch("models.bundledLoaders", "false"),
                         {"models": {"bundledLoaders": False}})

    def test_cli_generic_set_sends_bundled_loaders_put(self):
        for raw, expected in (("true", True), ("false", False)):
            client = Mock()
            client.request.return_value = _response({})
            args = argparse.Namespace(
                command="config", action="set",
                key="models.bundledLoaders", value=raw)
            execute(args, client)
            client.request.assert_called_once_with(
                "PUT", "/api/v1/settings",
                {"models": {"bundledLoaders": expected}})

    def test_gui_payload_accepted_by_server(self):
        # Mirror MLXBarSettingsView's setConfig("models.bundledLoaders", ...)
        # persistence target: the same PUT payload the CLI sends.
        public = self.store.update({"models": {"bundledLoaders": True}})
        self.assertTrue(public["models"]["bundledLoaders"])
        public = self.store.update({"models": {"bundledLoaders": False}})
        self.assertFalse(public["models"]["bundledLoaders"])

    def test_config_get_includes_bundled_loaders(self):
        # `mlxbarctl config get` (GET /api/v1/settings) must surface the
        # same key the SET paths write.
        client = Mock()
        client.request.return_value = _response(
            {"models": {"bundledLoaders": self.store.public()["models"]["bundledLoaders"]}})
        args = argparse.Namespace(command="config", action="get")
        document = execute(args, client)
        self.assertEqual(document["models"]["bundledLoaders"], False)
        client.request.assert_called_once_with("GET", "/api/v1/settings")


# ---------------------------------------------------------------------------
# 6. Catalog classifier version is not bumped by v5.0.2
# ---------------------------------------------------------------------------

class CatalogVersionPinnedTests(unittest.TestCase):
    def test_catalog_classifier_version_stays_4(self):
        """v5.0.2 changes no classifier rule, so the v5.0.1 bump stays."""
        self.assertEqual(state_module.CATALOG_CLASSIFIER_VERSION, "4")

    def test_settings_schema_version_stays_1(self):
        self.assertEqual(self.store_schema(), 1)

    @staticmethod
    def store_schema() -> int:
        from mlxbar.settings import DEFAULTS
        return int(DEFAULTS["schemaVersion"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
