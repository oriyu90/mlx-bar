"""v3.3.3 (TASK-005): 常駐モデルの起動時 preload が「無言で」走らない問題.

症状は「launchd 経由で起動したときだけ常駐モデルが読み込まれない。ワーカーも
生成されず、ログにも何も残らない」。調査の結果、起動時 preload
(`_preload_last_model`) の 3 つの skip 経路がどれも logger を呼ばず、
GUI も `skipped` を無視していたため、失敗がどこにも現れないことが原因だった。

根本原因は 2 つ:

* `DELETE /models/loaded`（GUI の「アンロード」）が立てる
  `api_autoload_suspended` を、`keepLoaded: true`（常駐に設定）が解除しなかった。
  「常駐にする」は「起動時に読む」という明示意思なので、ゲートを開け直す必要がある。
* 常駐プロファイルの `modelId` がカタログに無い場合を良性 skip として扱っていた。
  ID は `sha256(realpath)[:16]` 由来なので、モデルを移動しただけで常駐設定が
  永久に効かなくなる。これは失敗として報告する。

ここでは (1) 各判断がログに残ること、(2) 常駐設定がゲートを開け直すこと、
(3) 既存の「アンロード → 再起動で戻らない」契約が壊れていないことを固定する。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mlxbar.database import Database
from mlxbar.main import make_management_app
from mlxbar.settings import SettingsStore

ROOT = Path(__file__).resolve().parents[1]


class _FakeDatabase:
    def __init__(self, metadata: dict, models: list[dict]):
        self.metadata = metadata
        self.models = models

    def metadata_value(self, key):
        return self.metadata.get(key)

    def set_metadata_value(self, key, value):
        self.metadata[key] = value

    def list_models(self):
        return list(self.models)


class _FakeWorkers:
    def __init__(self):
        self.loaded = []

    async def load(self, model, engine=None, explicit=None, pin=True):
        self.loaded.append(model["id"])
        return {"id": model["id"]}


CATALOG = [{"id": "model-1", "name": "model-1", "engine": "mlx-lm"}]


def make_state(*, profiles, suspended="0", last_loaded=None, models=None):
    """Minimal AppState double with the surface `_preload_last_model` uses."""
    from mlxbar import main as main_module

    settings = SimpleNamespace(data={
        "models": {"pool": {"profiles": profiles}},
        "general": {"preloadLastModel": True},
    })
    state = SimpleNamespace(
        settings=settings,
        database=_FakeDatabase({"api_autoload_suspended": suspended,
                                "last_loaded_model_id": last_loaded},
                               CATALOG if models is None else models),
        workers=_FakeWorkers(),
        model_autoload_lock=asyncio.Lock(),
    )

    def set_preload_state(value, *, model_id=None, reason=None, code=None):
        state.preload_state = {"state": value, "modelId": model_id,
                               "reason": reason, "code": code, "at": 0.0}

    state.set_preload_state = set_preload_state
    return state, main_module


class PreloadLoggingTests(unittest.IsolatedAsyncioTestCase):
    """AC-1: every preload decision is visible in the coordinator log."""

    async def test_suspended_autoload_says_why_it_is_skipping(self):
        state, main_module = make_state(
            profiles=[{"modelId": "model-1", "keepLoaded": True}], suspended="1")
        with self.assertLogs("mlxbar.main", level="INFO") as logs:
            self.assertIsNone(main_module._preload_last_model(state))
        text = "\n".join(logs.output)
        self.assertIn("Preload skipped", text)
        self.assertIn("auto-load gate is still armed", text)
        self.assertEqual(state.preload_state["state"], "skipped")
        self.assertEqual(state.preload_state["reason"], "api_autoload_suspended")

    async def test_no_resident_target_is_logged_as_nothing_to_do(self):
        state, main_module = make_state(profiles=[])
        with self.assertLogs("mlxbar.main", level="INFO") as logs:
            self.assertIsNone(main_module._preload_last_model(state))
        self.assertIn("no resident model is configured", "\n".join(logs.output))
        self.assertEqual(state.preload_state["reason"], "no_resident_target")

    async def test_missing_resident_row_is_a_failure_not_a_silent_skip(self):
        state, main_module = make_state(
            profiles=[{"modelId": "ghost", "keepLoaded": True}])
        with self.assertLogs("mlxbar.main", level="INFO") as logs:
            await main_module._preload_last_model(state)
        text = "\n".join(logs.output)
        self.assertIn("ghost", text)
        self.assertIn("no longer in the model list", text)
        # The resident setting is broken, so the status must not read as a
        # benign "skipped" -- the menu bar turns this into a visible warning.
        self.assertEqual(state.preload_state["state"], "failed")
        self.assertEqual(state.preload_state["code"], "RESIDENT_MODEL_MISSING")
        self.assertEqual(state.preload_state["modelId"], "ghost")

    async def test_a_successful_preload_logs_the_start_and_the_duration(self):
        state, main_module = make_state(
            profiles=[{"modelId": "model-1", "keepLoaded": True}])
        with self.assertLogs("mlxbar.main", level="INFO") as logs:
            await main_module._preload_last_model(state)
        text = "\n".join(logs.output)
        self.assertIn("Preload starting: 1 resident, 0 last-used", text)
        self.assertIn("Preloaded model-1 in", text)
        self.assertEqual(state.preload_state["state"], "loaded")

    async def test_the_log_names_the_reason_for_a_transient_miss(self):
        state, main_module = make_state(profiles=[], last_loaded="gone")
        with self.assertLogs("mlxbar.main", level="INFO") as logs:
            await main_module._preload_last_model(state)
        text = "\n".join(logs.output)
        self.assertIn("last-used model gone", text)
        self.assertEqual(state.preload_state["state"], "skipped")
        self.assertEqual(state.preload_state["reason"], "model_not_in_catalog")


class ResidentAutoloadRearmTests(unittest.TestCase):
    """AC-2: declaring a resident re-opens the gate that unload closes.

    Compatibility: an unload with no residency change must still survive a
    restart (AC-7-3), so these tests also pin the *unchanged* behaviour.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.database = Database(self.root / "state.sqlite3")

        async def unload_model(model_id, force=False):
            return {"state": "unloaded", "id": model_id}

        async def unload_all():
            return {"state": "unloaded"}

        self.workers = SimpleNamespace(unload_model=unload_model,
                                       unload=unload_all, _slots={})
        self.state = SimpleNamespace(settings=self.settings, database=self.database,
                                     workers=self.workers)
        self.client = TestClient(make_management_app(self.state))

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def pin(self, model_id, **body):
        return self.client.put(
            "/api/v1/settings/models/pool/profiles/" + model_id,
            json={"keepLoaded": True, **body})

    def suspended(self):
        return self.database.metadata_value("api_autoload_suspended")

    def test_pinning_a_resident_clears_the_gate(self):
        self.database.set_metadata_value("api_autoload_suspended", "1")
        response = self.pin("model-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.suspended(), "0")

    def test_unpinning_does_not_touch_the_gate(self):
        self.pin("model-1")
        self.database.set_metadata_value("api_autoload_suspended", "1")
        response = self.client.put(
            "/api/v1/settings/models/pool/profiles/model-1",
            json={"keepLoaded": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.suspended(), "1")

    def test_a_rejected_profile_cannot_clear_the_gate(self):
        self.database.set_metadata_value("api_autoload_suspended", "1")
        response = self.client.put(
            "/api/v1/settings/models/pool/profiles/model-1",
            json={"keepLoaded": True, "replicas": 0})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.suspended(), "1")

    def test_unrelated_settings_writes_leave_the_gate_alone(self):
        self.database.set_metadata_value("api_autoload_suspended", "1")
        response = self.client.put("/api/v1/settings",
                                   json={"general": {"preloadLastModel": False}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.suspended(), "1")

    def test_a_settings_write_that_declares_a_resident_clears_the_gate(self):
        self.database.set_metadata_value("api_autoload_suspended", "1")
        response = self.client.put("/api/v1/settings", json={
            "models": {"pool": {"profiles": [{"modelId": "model-1",
                                              "keepLoaded": True}]}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.suspended(), "0")

    def test_an_all_models_unload_still_arms_the_gate(self):
        response = self.client.delete("/api/v1/models/loaded")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.suspended(), "1")


class _FakeJobs:
    def __init__(self):
        self.created = []

    def create(self, kind, work):
        self.created.append((kind, work))
        return {"id": "job-1"}


class CatalogScanGuardTests(unittest.IsolatedAsyncioTestCase):
    """AC-4: an unmounted model root must not destroy the catalog.

    The resident model lives on an external volume. A scan that runs before the
    volume mounts contributes no rows for it, `replace_models` deletes them, and
    the version markers are stamped -- so nothing rescans and the resident
    preload keeps failing with "not in the model list" until the markers change
    again. The fix keeps the catalog and leaves the markers alone.
    """

    def make_state(self, roots, rows):
        from mlxbar import state as state_module

        settings = SimpleNamespace(data={"models": {"roots": roots}},
                                   lm_studio_token=None)
        database = SimpleNamespace()
        database.list_models = lambda: list(rows)
        database.replaced = []
        database.replace_models = lambda models: database.replaced.append(models)
        database.markers = {}
        database.set_metadata_value = lambda key, value: database.markers.update({key: value})
        fake = SimpleNamespace(settings=settings, database=database, jobs=_FakeJobs())
        # `scan_job`'s worker calls the method on `self`; bind the real
        # implementation to the double so the guard is exercised, not stubbed.
        fake._roots_that_would_be_lost = (
            lambda: state_module.AppState._roots_that_would_be_lost(fake))
        return fake, state_module

    def test_an_unmounted_root_that_holds_rows_is_reported(self):
        fake, state_module = self.make_state(
            ["/nonexistent/mlx-models"],
            [{"id": "m", "path": "/nonexistent/mlx-models/org/model"}])
        self.assertEqual(
            state_module.AppState._roots_that_would_be_lost(fake),
            ["/nonexistent/mlx-models"])

    def test_a_root_that_never_held_rows_does_not_block_scanning(self):
        fake, state_module = self.make_state(
            ["/nonexistent/empty-root"],
            [{"id": "m", "path": "/elsewhere/model"}])
        self.assertEqual(state_module.AppState._roots_that_would_be_lost(fake), [])

    def test_a_readable_root_is_never_reported(self):
        fake, state_module = self.make_state([str(ROOT)], [{"id": "m", "path": str(ROOT)}])
        self.assertEqual(state_module.AppState._roots_that_would_be_lost(fake), [])

    async def test_the_scan_keeps_the_catalog_and_the_markers(self):
        fake, state_module = self.make_state(
            ["/nonexistent/mlx-models"],
            [{"id": "m", "path": "/nonexistent/mlx-models/org/model"}])
        job = state_module.AppState.scan_job(fake)
        self.assertEqual(job["id"], "job-1")

        async def update(_progress, _message):
            return None

        result = await fake.jobs.created[0][1](update)
        self.assertEqual(result["skipped"], "unavailable_roots")
        self.assertEqual(result["roots"], ["/nonexistent/mlx-models"])
        # Nothing was destroyed and no marker was advanced, so the next start
        # rescans once the volume is back.
        self.assertEqual(fake.database.replaced, [])
        self.assertEqual(fake.database.markers, {})



class VolumeGuardTests(unittest.IsolatedAsyncioTestCase):
    """AC-6: a volume that never answers must not hang the preload forever.

    macOS holds `open()` on an external volume until the Removable Volumes
    permission prompt is answered, and a launchd agent cannot show that
    prompt. The read has to give up and say why.
    """

    def make(self, timeout=0.1, cooldown=60.0):
        from mlxbar.io_guard import VolumeGuard

        return VolumeGuard(timeout=timeout, cooldown=cooldown)

    async def test_a_normal_read_returns_its_value(self):
        guard = self.make()
        self.assertEqual(await guard.read(lambda: 7), 7)
        self.assertFalse(guard.stalled())

    async def test_a_stalled_read_raises_the_volume_code(self):
        import time as time_module

        from mlxbar.errors import MLXBarError
        from mlxbar.io_guard import CODE

        guard = self.make(timeout=0.05)
        with self.assertLogs("mlxbar.io_guard", level="WARNING") as logs:
            with self.assertRaises(MLXBarError) as caught:
                await guard.read(time_module.sleep, 0.3, what="model config")
        self.assertEqual(caught.exception.code, CODE)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("リムーバブルボリューム", caught.exception.message)
        self.assertIn("model config", "\n".join(logs.output))
        self.assertTrue(guard.stalled())

    async def test_the_guard_fails_fast_while_stalled(self):
        import time as time_module

        from mlxbar.errors import MLXBarError
        from mlxbar.io_guard import CODE

        guard = self.make(timeout=0.05)
        with self.assertRaises(MLXBarError):
            await guard.read(time_module.sleep, 0.2)
        # The second call must not start another thread: the first one is
        # still blocked and cannot be cancelled.
        with self.assertRaises(MLXBarError) as caught:
            await guard.read(lambda: 1)
        self.assertEqual(caught.exception.code, CODE)
        self.assertEqual(guard.stall_reason(), "model metadata")

    async def test_the_stall_clears_after_the_cooldown(self):
        import asyncio as asyncio_module
        import time as time_module

        from mlxbar.errors import MLXBarError

        guard = self.make(timeout=0.05, cooldown=0.2)
        with self.assertRaises(MLXBarError):
            await guard.read(time_module.sleep, 0.2)
        self.assertTrue(guard.stalled())
        await asyncio_module.sleep(0.25)
        self.assertFalse(guard.stalled())
        self.assertEqual(await guard.read(lambda: "ok"), "ok")

    def test_the_supervisor_routes_its_metadata_reads_through_the_guard(self):
        source = (ROOT / "Coordinator" / "mlxbar" / "workers" / "supervisor.py"
                  ).read_text(encoding="utf-8")
        self.assertIn("self.volume_guard = VolumeGuard()", source)
        self.assertGreaterEqual(source.count("await self.volume_guard.read("), 3)
        # No unbounded metadata read may come back: those are the ones that hung.
        self.assertNotIn("await asyncio.to_thread(\n            self._detect_model_max_tokens", source)


class StartupSequenceTests(unittest.TestCase):
    """AC-5: the banner and the signal handlers exist before the long waits."""

    def setUp(self):
        self.source = (ROOT / "Coordinator" / "mlxbar" / "main.py").read_text(encoding="utf-8")
        self.serve = self.source[self.source.index("async def serve("):]

    def test_the_startup_banner_names_the_build_and_the_process(self):
        self.assertIn("MLXBar coordinator %s starting pid=", self.serve)

    def test_the_banner_version_symbol_is_imported(self):
        # A NameError in the first lines of `serve()` kills the coordinator
        # before anything is logged, so only the e2e suite would notice.
        from mlxbar import main as main_module

        self.assertIsInstance(getattr(main_module, "__version__", None), str)

    def test_signal_handlers_are_registered_before_the_scan_wait(self):
        handlers = self.serve.index("add_signal_handler")
        scan_wait = self.serve.index("await scan_task")
        self.assertLess(handlers, scan_wait)

    def test_a_pending_scan_says_why_nothing_is_loading(self):
        self.assertIn('_record_preload(state, "pending", reason="catalog_scan")', self.serve)



class ShippedWiringTests(unittest.TestCase):
    """AC-3: the fix is wired into the shipped paths, not only into helpers."""

    def test_the_preload_source_logs_every_branch(self):
        source = (ROOT / "Coordinator" / "mlxbar" / "main.py").read_text(encoding="utf-8")
        start = source.index("def _preload_last_model")
        end = source.index("def _omlx_auto_connect_task", start)
        body = source[start:end]
        # Three skip decisions + start + success + failure, each audible.
        self.assertGreaterEqual(body.count("log.info("), 4)
        self.assertGreaterEqual(body.count("log.warning("), 2)
        self.assertIn("RESIDENT_MODEL_MISSING", body)

    def test_the_new_code_has_bilingual_text(self):
        text = (ROOT / "Sources" / "MLXBar" / "Services" / "ErrorText.swift"
                ).read_text(encoding="utf-8")
        self.assertIn('"RESIDENT_MODEL_MISSING"', text)

    def test_the_gui_no_longer_hides_every_skip(self):
        swift = (ROOT / "Sources" / "MLXBar" / "MenuBar" / "MenuBarViewModel.swift"
                 ).read_text(encoding="utf-8")
        start = swift.index("var preloadStatusLine")
        body = swift[start:start + 1600]
        self.assertIn('case "skipped" where preload.reason == "api_autoload_suspended"', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
