"""v3.1.0 の欠落テスト（WP-911）: 起動順序・preload 状態・import スモーク.

v3.1.0 は `git show 92cd5e2 --stat -- Tests` が空のままリリースされ、DESIGN が
参照する `test_v3_1_0_*.py` は存在しなかった。さらに中間コミット `92cd5e2` は
単体で `import mlxbar.main` が失敗する（classifier の縮小と supervisor の
import の不整合）。ここでは「起動時の scan 待ち → preload」の順序と、
preload 結果が状態として残ることを固定する。
"""
from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from mlxbar.errors import MLXBarError

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "Coordinator" / "mlxbar" / "main.py"


class ImportSmokeTests(unittest.TestCase):
    """AC-T-7: the module the service entry point imports must import."""

    def test_main_module_imports(self):
        import importlib

        module = importlib.import_module("mlxbar.main")
        self.assertTrue(hasattr(module, "_preload_last_model"))
        self.assertTrue(hasattr(module, "make_management_app"))

    def test_supervisor_imports_the_classifier_it_calls(self):
        from mlxbar.catalog import classifier
        from mlxbar.workers import supervisor

        self.assertTrue(hasattr(classifier, "detect_engine"))
        self.assertTrue(hasattr(supervisor, "WorkerSupervisor"))


class StartupOrderTests(unittest.TestCase):
    def test_scan_is_awaited_before_the_preload(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        await_scan = source.index("await scan_task")
        # The first mention of `_preload_last_model` is its definition; the
        # call site must come *after* the scan is awaited.
        preload = source.index("_preload_last_model(state)", await_scan)
        self.assertLess(await_scan, preload)


class _FakeDatabase:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    def metadata_value(self, key):
        return self.metadata.get(key)

    def set_metadata_value(self, key, value):
        self.metadata[key] = value

    def list_models(self):
        return [{"id": "model-1", "name": "model-1", "engine": "mlx-lm"},
                {"id": "model-2", "name": "model-2", "engine": "mlx-lm"}]


class _FakeWorkers:
    def __init__(self, failing=()):
        self.loaded = []
        self.failing = set(failing)

    async def load(self, model, engine=None, explicit=None, pin=True):
        model_id = model["id"]
        if model_id in self.failing:
            raise MLXBarError("MEMORY_HEADROOM_INSUFFICIENT", "空きメモリ不足", 503, True)
        self.loaded.append(model_id)
        return {"id": model_id}


class PreloadStateTests(unittest.IsolatedAsyncioTestCase):
    """AC-3-3: every preload outcome is recorded, not just logged."""

    def make_state(self, *, profiles, last_loaded=None, suspended="0", failing=()):
        from mlxbar import main as main_module

        settings = SimpleNamespace(data={
            "models": {"pool": {"profiles": profiles}},
            "general": {"preloadLastModel": True},
        })
        metadata = {"api_autoload_suspended": suspended,
                    "last_loaded_model_id": last_loaded}
        state = SimpleNamespace(settings=settings,
                                database=_FakeDatabase(metadata),
                                workers=_FakeWorkers(failing),
                                model_autoload_lock=asyncio.Lock())

        # The real AppState owns this recorder (Coordinator/mlxbar/state.py);
        # the fake needs the same one-method surface `_record_preload` uses.
        def set_preload_state(value, *, model_id=None, reason=None, code=None):
            state.preload_state = {"state": value, "modelId": model_id,
                                   "reason": reason, "code": code, "at": 0.0}

        state.set_preload_state = set_preload_state
        return state, main_module

    async def test_no_target_is_recorded_as_skipped(self):
        state, main_module = self.make_state(profiles=[])
        task = main_module._preload_last_model(state)
        self.assertIsNone(task)
        self.assertEqual(state.preload_state["state"], "skipped")
        self.assertEqual(state.preload_state["reason"], "no_resident_target")

    async def test_suspended_autoload_is_recorded_as_skipped(self):
        state, main_module = self.make_state(
            profiles=[{"modelId": "model-1", "keepLoaded": True}], suspended="1")
        self.assertIsNone(main_module._preload_last_model(state))
        self.assertEqual(state.preload_state["state"], "skipped")
        self.assertEqual(state.preload_state["reason"], "api_autoload_suspended")

    async def test_success_ends_in_loaded(self):
        state, main_module = self.make_state(
            profiles=[{"modelId": "model-1", "keepLoaded": True}])
        task = main_module._preload_last_model(state)
        self.assertIsNotNone(task)
        await task
        self.assertEqual(state.preload_state["state"], "loaded")
        self.assertEqual(state.preload_state["modelId"], "model-1")
        self.assertEqual(state.workers.loaded, ["model-1"])

    async def test_failure_ends_in_failed_with_the_error_code(self):
        state, main_module = self.make_state(
            profiles=[{"modelId": "model-1", "keepLoaded": True}],
            failing={"model-1"})
        task = main_module._preload_last_model(state)
        await task
        self.assertEqual(state.preload_state["state"], "failed")
        self.assertEqual(state.preload_state["code"], "MEMORY_HEADROOM_INSUFFICIENT")
        self.assertIn("空きメモリ", state.preload_state["reason"])

    async def test_a_missing_catalog_row_does_not_fail_the_whole_preload(self):
        state, main_module = self.make_state(
            profiles=[{"modelId": "ghost", "keepLoaded": True},
                      {"modelId": "model-2", "keepLoaded": True}])
        await main_module._preload_last_model(state)
        # The ghost is skipped, the real one still loads, and the last recorded
        # state is the successful one.
        self.assertEqual(state.workers.loaded, ["model-2"])
        self.assertEqual(state.preload_state["state"], "loaded")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
