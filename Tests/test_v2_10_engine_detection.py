"""v2.10.0: the vision-engine chain (classify -> guard -> load -> self-heal).

Covers the four layers the v2.10.0 VLM fix touches:

* ``catalog/classifier.py`` — the corrected VLM heuristics and the load-time
  ``detect_engine`` re-derivation,
* the mlx-lm worker's vision guard (``common/vision.py`` + adapter + server
  classification into ``MODEL_INCOMPATIBLE``),
* ``supervisor.load``'s auto/explicit chain (strict explicit choice, symmetric
  auto fallback, `allow_vlm` only for explicit picks),
* and the pool's post-load catalog self-heal plus the
  ``Database.update_model_classification`` row writer.

Everything runs on fake model directories — no torch, no mlx, no real
runtime installs. The worker-side guard reuses the marker sets duplicated
from the coordinator classifier; a dedicated test pins both copies in sync.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from fastapi.testclient import TestClient  # noqa: E402

from mlxbar.catalog import classifier  # noqa: E402
from mlxbar.catalog.classifier import (  # noqa: E402
    classify,
    detect_engine,
)
from mlxbar.database import Database  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.workers import model_pool as model_pool_module  # noqa: E402
from mlxbar.workers.model_pool import ModelPoolSupervisor  # noqa: E402
from mlxbar.workers.supervisor import WorkerSupervisor  # noqa: E402
import mlxbar.state as state_module  # noqa: E402

from common import vision as worker_vision  # noqa: E402
from common.server import create_app  # noqa: E402
from common.vision import GUARD_TAG  # noqa: E402
from mlx_lm_worker.adapter import MLXLMAdapter  # noqa: E402

# --- fake model fixtures ---------------------------------------------------

TEXT_CONFIG = {"model_type": "llama", "architectures": ["LlamaForCausalLM"]}
QWEN_VL_CONFIG = {
    "model_type": "qwen2_vl",
    "architectures": ["Qwen2VLForConditionalGeneration"],
}
# A real vision checkpoint that used to fall through every v2.9.0 rule: no
# vision-named architecture, vision_config present, no processor file.
VISION_ONLY_CONFIG = {"model_type": "gemma3", "vision_config": {"hidden_size": 64}}
LAGUNA_CONFIG = {"model_type": "llama", "architectures": ["LagunaForCausalLM"]}
FALSE_POSITIVE_CONFIG = {"model_type": "smollm2", "architectures": ["SmolLM2ForCausalLM"]}
GGUF_BYTES = b"GGUF" + b"\x00" * 12


def make_model_dir(base: Path, name: str, config: dict, *,
                   processor: bool = False, tokenizer_config: dict | None = None,
                   gguf: bool = False, complete: bool = True) -> Path:
    """A model directory good enough for every classification heuristics."""
    path = base / name
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if gguf:
        (path / "model.gguf").write_bytes(GGUF_BYTES)
        return path
    if complete:
        (path / "model.safetensors").write_bytes(b"\x00" * 16)
        (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    if processor:
        (path / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    if tokenizer_config is not None:
        (path / "tokenizer_config.json").write_text(
            json.dumps(tokenizer_config), encoding="utf-8")
    return path


def db_row(model_id: str, path: Path, engine: str = "mlx-lm", fmt: str = "mlx_lm",
           modalities: list[str] | None = None, confidence: float = 0.8) -> dict:
    return {"id": model_id, "source": "local", "name": model_id, "path": str(path),
            "provider_key": None, "format": fmt, "engine": engine,
            "modalities": modalities if modalities is not None else ["text"],
            "confidence": confidence, "reason": "previous scan", "size_bytes": 1024}


# --- classifier ------------------------------------------------------------

class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gguf_routes_to_lm_studio(self):
        result = classify(make_model_dir(self.tmp, "gguf-model", TEXT_CONFIG, gguf=True))
        self.assertEqual(result["engine"], "lm-studio")
        self.assertEqual(result["format"], "gguf")

    def test_incomplete_directory_is_unknown(self):
        path = make_model_dir(self.tmp, "broke", TEXT_CONFIG, complete=False)
        result = classify(path)
        self.assertEqual(result["format"], "unknown")
        self.assertIsNone(result["engine"])

    def test_plain_text_model_is_mlx_lm(self):
        result = classify(make_model_dir(self.tmp, "text", TEXT_CONFIG))
        self.assertEqual(result["engine"], "mlx-lm")
        self.assertEqual(result["modalities"], ["text"])
        self.assertEqual(result["confidence"], 0.8)

    def test_vlm_architecture_model_type_is_detected(self):
        result = classify(make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["modalities"], ["text", "image"])
        self.assertEqual(result["confidence"], 0.95)
        self.assertIn("qwen2_vl", result["reason"])

    def test_vlm_architecture_in_architectures_list_is_detected(self):
        config = {"model_type": "llama", "architectures": ["LlavaForConditionalGeneration"]}
        result = classify(make_model_dir(self.tmp, "llava", config))
        self.assertEqual(result["engine"], "mlx-vlm")

    def test_vision_config_without_processor_routes_to_vlm(self):
        """The v2.10.0 core fix: vision config + no processor file used to be
        pinned to mlx-lm; now it routes to mlx-vlm (lower confidence, the
        load-time chain re-verifies)."""
        result = classify(make_model_dir(self.tmp, "gemma", VISION_ONLY_CONFIG))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["confidence"], 0.6)
        self.assertIn("processor設定ファイルなし", result["reason"])

    def test_vision_config_plus_processor_is_higher_confidence(self):
        result = classify(make_model_dir(
            self.tmp, "gemma-proc", VISION_ONLY_CONFIG, processor=True))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["confidence"], 0.85)

    def test_processor_class_evidence_is_reported(self):
        result = classify(make_model_dir(
            self.tmp, "gemma-tok", VISION_ONLY_CONFIG,
            tokenizer_config={"processor_class": "Qwen2VLProcessor"}))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["confidence"], 0.85)
        self.assertIn("Qwen2VLProcessor", result["reason"])

    def test_processor_evidence_alone_stays_text(self):
        """A text architecture with a processor file but no vision config is
        not flipped (conservative: processor helpers exist for text models
        too)."""
        result = classify(make_model_dir(self.tmp, "text-proc", TEXT_CONFIG, processor=True))
        self.assertEqual(result["engine"], "mlx-lm")

    def test_mlx_vlm_text_only_families_route_to_mlx_vlm(self):
        result = classify(make_model_dir(self.tmp, "laguna",
                                         {"model_type": "laguna",
                                          "architectures": ["LagunaForCausalLM"]}))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["modalities"], ["text"])

    def test_lookalike_text_names_are_not_flagged(self):
        """'smollm2' shares a prefix with the 'smolvlm' marker and must stay
        text: the substring match has never been allowed to over-match."""
        result = classify(make_model_dir(self.tmp, "smol", FALSE_POSITIVE_CONFIG))
        self.assertEqual(result["engine"], "mlx-lm")


# --- load-time re-derivation -----------------------------------------------

class DetectEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_config_returns_none(self):
        path = self.tmp / "empty"
        path.mkdir()
        self.assertIsNone(detect_engine(path))

    def test_unreadable_config_returns_none(self):
        path = self.tmp / "bad"
        path.mkdir()
        (path / "config.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(detect_engine(path))

    def test_non_dict_config_returns_none(self):
        path = self.tmp / "list"
        path.mkdir()
        (path / "config.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(detect_engine(path))

    def test_rederivation_needs_only_config(self):
        """Unlike classify(), the load-time form judges a directory with a
        lone config.json: the caller already resolved the weights."""
        path = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG, complete=False)
        self.assertEqual(detect_engine(path), "mlx-vlm")
        text = make_model_dir(self.tmp, "text", TEXT_CONFIG, complete=False)
        self.assertEqual(detect_engine(text), "mlx-lm")

    def test_vision_config_without_processor_rederives_to_vlm(self):
        path = make_model_dir(self.tmp, "gemma", VISION_ONLY_CONFIG)
        self.assertEqual(detect_engine(path), "mlx-vlm")


# --- worker-side guard ------------------------------------------------------

class MarkerSyncTests(unittest.TestCase):
    """common/vision.py duplicates the classifier's markers on purpose (the
    workers are a separate interpreter); the copies must never drift."""

    def test_marker_sets_match_coordinator(self):
        self.assertEqual(frozenset(worker_vision.VISION_KEYS),
                         frozenset(classifier.VISION_KEYS))
        self.assertEqual(frozenset(worker_vision.ARCH_VISION_TOKENS),
                         frozenset(classifier.ARCH_VISION_TOKENS))
        self.assertEqual(tuple(worker_vision.ARCH_VISION_MARKERS),
                         tuple(classifier.ARCH_VISION_MARKERS))
        self.assertEqual(worker_vision._TOKEN_SPLIT.pattern,
                         classifier._TOKEN_SPLIT.pattern)


class WorkerGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        self.text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_adapter_guard_raises_tagged_error(self):
        adapter = MLXLMAdapter()
        with self.assertRaises(RuntimeError) as raised:
            adapter.load(str(self.vlm_dir))
        self.assertIn(GUARD_TAG, str(raised.exception))

    def test_adapter_explicit_allow_vlm_skips_guard(self):
        adapter = MLXLMAdapter()
        try:
            adapter.load(str(self.vlm_dir), allow_vlm=True)
        except Exception as exc:  # noqa: BLE001 - mlx-lm is absent in tests
            self.assertNotIn(GUARD_TAG, str(exc))

    def test_adapter_text_model_never_hits_guard(self):
        adapter = MLXLMAdapter()
        try:
            adapter.load(str(self.text_dir))
        except Exception as exc:  # noqa: BLE001
            self.assertNotIn(GUARD_TAG, str(exc))

    def test_server_classifies_guard_as_model_incompatible(self):
        client = TestClient(create_app(MLXLMAdapter()))
        response = client.post("/rpc", json={"protocol_version": 1,
                                             "method": "load",
                                             "params": {"path": str(self.vlm_dir)}})
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["code"], "MODEL_INCOMPATIBLE")
        self.assertIn(GUARD_TAG, data.get("detail", ""))
        self.assertIn("vision", data.get("message", ""))

    def test_server_allow_vlm_passes_the_guard(self):
        client = TestClient(create_app(MLXLMAdapter()))
        response = client.post("/rpc", json={"protocol_version": 1,
                                             "method": "load",
                                             "params": {"path": str(self.vlm_dir),
                                                        "allow_vlm": True}})
        # The guard did not fire; the failure is the (test-env) missing
        # mlx-lm runtime, not a model classification problem.
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "RUNTIME_NOT_INSTALLED")

    def test_server_text_model_is_not_guarded(self):
        client = TestClient(create_app(MLXLMAdapter()))
        response = client.post("/rpc", json={"protocol_version": 1,
                                             "method": "load",
                                             "params": {"path": str(self.text_dir)}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "RUNTIME_NOT_INSTALLED")


# --- supervisor engine chain ------------------------------------------------

def _pool_settings(**pool) -> types.SimpleNamespace:
    return types.SimpleNamespace(data={
        "generation": {"loadTimeoutSeconds": 5},
        "models": {"pool": {"enabled": True, **pool}},
    })


class SupervisorEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = types.SimpleNamespace(data={
            "generation": {"loadTimeoutSeconds": 5},
            "models": {"lmStudio": {"baseUrl": "http://127.0.0.1:9999"}},
        })
        self.sup = WorkerSupervisor(self.tmp, self.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def instrument(self, fails: bool = True):
        """Replace the runtime spawn + RPC with recorders that fail every
        load with MODEL_INCOMPATIBLE (the fallback trigger)."""
        starts: list[str] = []
        calls: list[tuple[str, dict]] = []

        async def fake_start(engine: str) -> None:
            starts.append(engine)

        async def fake_call(method: str, params: dict, timeout=None) -> dict:
            calls.append((method, dict(params)))
            if fails:
                raise MLXBarError("MODEL_INCOMPATIBLE", "runtime load failed", 400)
            return {"type": "completed", "capabilities": {}}

        self.sup._start_worker = fake_start  # type: ignore[method-assign]
        self.sup._call = fake_call  # type: ignore[method-assign]
        return starts, calls

    def test_auto_rederives_vlm_and_falls_back(self):
        """A stale catalog row (`engine: mlx-lm` on a vision directory) is
        re-derived at load time; the auto chain then retries symmetrically
        across the engine boundary."""
        vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        starts, calls = self.instrument()
        model = {"id": "m1", "name": "M", "path": str(vlm_dir), "engine": "mlx-lm"}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, None))
        self.assertEqual(starts, ["mlx-vlm", "mlx-lm"])
        self.assertEqual([call[0] for call in calls], ["load", "load"])
        self.assertFalse(calls[0][1].get("allow_vlm"))
        self.assertFalse(calls[1][1].get("allow_vlm"))

    async def _load(self, model: dict, engine: str | None) -> dict:
        return await self.sup.load(model, engine)

    def test_auto_keyword_matches_none(self):
        vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        starts, _ = self.instrument()
        model = {"id": "m1", "name": "M", "path": str(vlm_dir), "engine": "mlx-lm"}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, "auto"))
        self.assertEqual(starts, ["mlx-vlm", "mlx-lm"])

    def test_auto_text_model_tries_mlx_lm_first(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts, calls = self.instrument()
        model = {"id": "m2", "name": "T", "path": str(text_dir), "engine": "mlx-lm"}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, None))
        self.assertEqual(starts, ["mlx-lm", "mlx-vlm"])
        self.assertFalse(calls[0][1].get("allow_vlm"))

    def test_explicit_engine_is_strict(self):
        """An explicit engine choice never re-derives, never falls back, and
        never delegates to LM Studio — even for a provider-backed model."""
        vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        starts, calls = self.instrument()
        model = {"id": "m3", "name": "M", "path": str(vlm_dir),
                 "engine": "mlx-vlm", "provider_key": "lmstudio/stale"}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, "mlx-lm"))
        self.assertEqual(starts, ["mlx-lm"])  # one candidate only
        self.assertEqual(len(calls), 1)
        # Guard bypass: the user asked for mlx-lm on purpose.
        self.assertTrue(calls[0][1].get("allow_vlm"))

    def test_explicit_engine_sends_allow_vlm(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts, calls = self.instrument()
        model = {"id": "m4", "name": "T", "path": str(text_dir)}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, "mlx-vlm"))
        self.assertEqual(starts, ["mlx-vlm"])
        self.assertTrue(calls[0][1].get("allow_vlm"))

    def test_explicit_failure_surfaces_as_incompatible(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts, _ = self.instrument()
        model = {"id": "m5", "name": "T", "path": str(text_dir),
                 "provider_key": "lmstudio/stale"}
        with self.assertRaises(MLXBarError) as raised:
            asyncio.run(self._load(model, "mlx-lm"))
        self.assertEqual(raised.exception.code, "MODEL_INCOMPATIBLE")
        self.assertIsNone(self.sup.loaded)

    def test_unknown_engine_refuses_before_worker_start(self):
        starts, _ = self.instrument()
        model = {"id": "m6", "name": "X", "path": str(self.tmp)}
        with self.assertRaises(MLXBarError) as raised:
            asyncio.run(self._load(model, "dreamlab"))
        self.assertIn("利用可能なエンジン", str(raised.exception))
        self.assertEqual(starts, [])

    def test_auto_rescues_with_lm_studio_for_providers(self):
        """The auto chain (and only the auto chain) still has the LM Studio
        delegation for provider-backed models after both native engines fail."""
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        self.instrument()
        model = {"id": "m7", "name": "T", "path": str(text_dir),
                 "engine": "mlx-lm", "provider_key": "lmstudio/foo"}

        async def fake_lmstudio(m, timeout):
            return {"instance_id": "inst-1"}

        self.sup._load_lmstudio = fake_lmstudio  # type: ignore[method-assign]
        loaded = asyncio.run(self._load(model, None))
        self.assertEqual(loaded["engine"], "lm-studio")
        self.assertEqual(loaded.get("provider_instance_id"), "inst-1")

    def test_explicit_never_delegates_to_lm_studio(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts, _ = self.instrument()
        calls_delegate = []

        async def fake_lmstudio(m, timeout):
            calls_delegate.append(1)
            return {"instance_id": "inst-1"}

        self.sup._load_lmstudio = fake_lmstudio  # type: ignore[method-assign]
        model = {"id": "m8", "name": "T", "path": str(text_dir),
                 "provider_key": "lmstudio/foo"}
        with self.assertRaises(MLXBarError):
            asyncio.run(self._load(model, "mlx-lm"))
        self.assertEqual(calls_delegate, [])


# --- pool self-heal ---------------------------------------------------------

class PoolSelfHealTests(unittest.TestCase):
    """`_load_replica` hand-checks the auto/explicit distinction (full
    `load()` is exercised elsewhere; here the focus is the catalog write)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        self.text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pool(self, model: dict) -> tuple[Database, ModelPoolSupervisor, Mock]:
        db = Database(self.tmp / "state.sqlite3")
        db.replace_models([db_row(model["id"], self.vlm_dir)])
        pool = ModelPoolSupervisor(self.tmp, _pool_settings(), db)
        spy = Mock(wraps=pool._refresh_catalog_classification)
        pool._refresh_catalog_classification = spy  # type: ignore[method-assign]
        return db, pool, spy

    def fake_worker_downstream(self):
        """Make SingleWorkerSupervisor.load return a successful mlx-vlm load
        and silence the post-load memory RPC. Plain async functions are used
        (not Mocks) so the real descriptor protocol binds ``self``."""

        async def fake_load(self_worker, model, engine=None, explicit=None):
            return {"id": model.get("id"), "engine": "mlx-vlm", "state": "loaded",
                    "capabilities": {"memoryLimits": {
                        "set_memory_limit": self_worker.memory_limit_bytes}}}

        async def fake_call(self_worker, method, params, timeout=None):
            return {"memory": {}}

        return fake_load, fake_call

    def patched_worker(self, load_mock, call_mock):
        load_patch = patch.object(model_pool_module.SingleWorkerSupervisor,
                                  "load", load_mock)
        call_patch = patch.object(model_pool_module.SingleWorkerSupervisor,
                                  "_call", call_mock)
        return load_patch, call_patch

    def load_replica(self, pool, model, engine):
        async def run():
            return await pool._load_replica(model, engine, str(model.get("id")),
                                            0, pin=True)
        return asyncio.run(run())

    def test_auto_load_rewrites_stale_row(self):
        model = {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-lm", "size_bytes": 1024}
        db, pool, spy = self.make_pool(model)
        load_mock, call_mock = self.fake_worker_downstream()
        load_patch, call_patch = self.patched_worker(load_mock, call_mock)
        with load_patch, call_patch:
            result = self.load_replica(pool, model, None)
        self.assertEqual(result["engine"], "mlx-vlm")
        spy.assert_called_once()
        row = db.get_model("m1")
        self.assertEqual(row["engine"], "mlx-vlm")
        self.assertEqual(row["format"], "mlx_vlm")
        self.assertEqual(row["modalities"], ["text", "image"])
        self.assertEqual(row["confidence"], 0.7)
        self.assertIn("ロード時の自動再検出", row["reason"])

    def test_explicit_load_never_rewrites(self):
        model = {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-vlm", "size_bytes": 1024}
        db, pool, spy = self.make_pool(model)
        load_mock, call_mock = self.fake_worker_downstream()
        load_patch, call_patch = self.patched_worker(load_mock, call_mock)
        with load_patch, call_patch:
            self.load_replica(pool, model, "mlx-lm")
        spy.assert_not_called()
        row = db.get_model("m1")
        # The one-off override left the catalog untouched.
        self.assertEqual(row["engine"], "mlx-lm")
        self.assertEqual(row["confidence"], 0.8)

    def test_matching_auto_load_is_not_rewritten(self):
        model = {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-vlm", "size_bytes": 1024}
        db, pool, spy = self.make_pool(model)
        load_mock, call_mock = self.fake_worker_downstream()
        load_patch, call_patch = self.patched_worker(load_mock, call_mock)
        with load_patch, call_patch:
            self.load_replica(pool, model, None)
        spy.assert_not_called()

    def test_self_heal_never_surfaces_failure(self):
        model = {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-lm", "size_bytes": 1024}
        db, pool, spy = self.make_pool(model)
        with patch.object(model_pool_module, "_classify_catalog_model",
                          side_effect=OSError("disk gone")):
            # Must not raise: the load already succeeded.
            pool._refresh_catalog_classification(model, {"engine": "mlx-vlm"})
        row = db.get_model("m1")
        self.assertEqual(row["engine"], "mlx-lm")

    def test_self_heal_skips_without_database_or_id(self):
        pool = ModelPoolSupervisor(self.tmp, _pool_settings(), None)
        model = {"id": "m9", "name": "M", "path": str(self.vlm_dir), "engine": "mlx-lm"}
        pool._refresh_catalog_classification(model, {"engine": "mlx-vlm"})
        # A junk database object is not trusted either: the failure is
        # logged and swallowed, never surfaced to the load chain.
        pool.database = object()  # truthy but unusable
        pool._refresh_catalog_classification({"name": "no-id"}, {"engine": "mlx-vlm"})
        pool._refresh_catalog_classification(model, {"engine": "mlx-vlm"})


# --- database row writer -----------------------------------------------------

class DatabaseClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        self.vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        self.db = Database(self.tmp / "state.sqlite3")
        self.db.replace_models([db_row("m1", self.vlm_dir),
                                db_row("m2", self.text_dir)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.db.close()

    def test_updates_existing_row(self):
        result = self.db.update_model_classification(
            "m1", format="mlx_vlm", engine="mlx-vlm",
            modalities=["text", "image"], confidence=0.7,
            reason="ロード時の自動再検出")
        self.assertTrue(result)
        row = self.db.get_model("m1")
        self.assertEqual(row["engine"], "mlx-vlm")
        self.assertEqual(row["format"], "mlx_vlm")
        self.assertEqual(row["modalities"], ["text", "image"])
        self.assertEqual(row["confidence"], 0.7)
        self.assertIn("ロード時の自動再検出", row["reason"])

    def test_leaves_other_rows_untouched(self):
        self.db.update_model_classification(
            "m1", format="mlx_vlm", engine="mlx-vlm",
            modalities=["text", "image"], confidence=0.7, reason="r")
        untouched = self.db.get_model("m2")
        self.assertEqual(untouched["engine"], "mlx-lm")
        self.assertEqual(untouched["confidence"], 0.8)

    def test_missing_row_returns_false(self):
        self.assertFalse(self.db.update_model_classification(
            "ghost", format="mlx_lm", engine="mlx-lm",
            modalities=["text"], confidence=0.8, reason="r"))


class VersionMarkerTests(unittest.TestCase):
    def test_classifier_version_bumped(self):
        """A changed classifier must rescan existing installs; "3" is the
        v2.10.0 VLM-heuristics bump."""
        self.assertEqual(state_module.CATALOG_CLASSIFIER_VERSION, "3")


if __name__ == "__main__":
    unittest.main()
