"""v4.0.0 / TASK-007: preload truth, settings consistency, error text.

WP-1 (FIND-02, FIND-16): `MLXBarError` is a dataclass, so every field lands in
`BaseException.args` and `str(exc)` is the *tuple* repr
`('CODE', 'text', 503, True)`. Every place that put `str(exc)` into an API
payload therefore shipped that tuple to the GUI / CLI / diagnostics. The tests
below pin the shared helper and the shipped call sites.

The remaining WP-2 / WP-4 / WP-7 cases live in this same module (one file per
work package group, so the pool tests stay separate).
"""
from __future__ import annotations

import asyncio
import contextlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from mlxbar.errors import MLXBarError, describe_error

ROOT = Path(__file__).resolve().parents[1]


class DescribeErrorTests(unittest.TestCase):
    """WP-1: the message is the message, never the args tuple."""

    def test_mlxbar_error_describes_its_message(self):
        exc = MLXBarError("MODEL_VOLUME_UNREADABLE", "外付けボリュームを読めません", 503, True)
        # The bug: BaseException.__str__ returns str(self.args) for len(args) != 1.
        self.assertEqual(
            str(exc), "('MODEL_VOLUME_UNREADABLE', '外付けボリュームを読めません', 503, True)")
        self.assertEqual(describe_error(exc), "外付けボリュームを読めません")

    def test_a_plain_exception_still_describes_itself(self):
        self.assertEqual(describe_error(RuntimeError("boom")), "boom")

    def test_an_empty_message_falls_back_to_the_class_name(self):
        self.assertEqual(describe_error(MLXBarError("X", "", 500)), "X")
        self.assertEqual(describe_error(RuntimeError()), "RuntimeError")


def _preload_state_double(workers, *, profiles):
    """Minimal AppState double for `_preload_last_model` (see TASK-005 tests)."""
    from mlxbar import main as main_module

    settings = SimpleNamespace(data={
        "models": {"pool": {"profiles": profiles}},
        "general": {"preloadLastModel": False},
    })
    database = SimpleNamespace(
        metadata_value=lambda key: "0" if key == "api_autoload_suspended" else None,
        list_models=lambda: [{"id": "model-1", "name": "model-1", "engine": "mlx-lm"}],
    )
    state = SimpleNamespace(
        settings=settings, database=database, workers=workers,
        model_autoload_lock=asyncio.Lock(),
        preload_state={"state": "pending", "modelId": None, "reason": None,
                       "code": None, "at": None},
    )

    def set_preload_state(value, *, model_id=None, reason=None, code=None):
        state.preload_state = {"state": value, "modelId": model_id,
                               "reason": reason, "code": code, "at": 0.0}

    state.set_preload_state = set_preload_state
    return state, main_module


class PreloadReasonTextTests(unittest.IsolatedAsyncioTestCase):
    """WP-1 / FIND-02: `/status.preload.reason` carries the message."""

    async def test_a_failed_resident_preload_reports_its_message(self):
        class Workers:
            async def load(self, model, pin=True, **kwargs):
        # v5.1.0: the preload passes `origin=` so the pool knows the load
        # is an explicit one; this fake only counts calls.
                raise MLXBarError("MODEL_VOLUME_UNREADABLE",
                                  "外付けボリュームへのアクセスが許可されていません", 503, True)

        state, main_module = _preload_state_double(
            Workers(), profiles=[{"modelId": "model-1", "keepLoaded": True}])
        # The bounded retry is covered by its own tests; this one pins the text
        # of the first failure, so it must not wait 30s x 3.
        await main_module._preload_last_model(state, retry_interval=0, retry_attempts=0)

        self.assertEqual(state.preload_state["state"], "failed")
        self.assertEqual(state.preload_state["code"], "MODEL_VOLUME_UNREADABLE")
        self.assertEqual(state.preload_state["reason"],
                         "外付けボリュームへのアクセスが許可されていません")
        self.assertNotIn("('MODEL_VOLUME_UNREADABLE'", state.preload_state["reason"])


class ShippedCallSiteTests(unittest.TestCase):
    """WP-1: the four FIND-16 sites go through the helper, not str(exc).

    A source guard rather than four fixtures: these are one-line payload
    fields, and a behavioural test would have to boot the CLI, the update
    service and an SSE stream to observe the same string.
    """

    def _assert_uses_helper(self, relative: str, needle: str):
        source = (ROOT / relative).read_text(encoding="utf-8")
        start = source.index(needle)
        body = source[start:start + 700]
        self.assertIn("describe_error(", body,
                      f"{relative}: {needle} must use describe_error()")

    def test_cli_reports_the_load_error_message(self):
        self._assert_uses_helper("Coordinator/mlxbar/cli.py", 'result["loadError"]')

    def test_update_service_reports_the_failure_message(self):
        self._assert_uses_helper("Coordinator/mlxbar/runtimes/service.py",
                                 '"UPDATE_FAILED"')

    def test_streaming_error_frame_reports_the_message(self):
        self._assert_uses_helper("Coordinator/mlxbar/api/management.py",
                                 '"code": "INTERNAL_ERROR"')

    def test_main_preload_uses_the_helper(self):
        source = (ROOT / "Coordinator" / "mlxbar" / "main.py").read_text(encoding="utf-8")
        start = source.index("def _preload_last_model")
        end = source.index("def _omlx_auto_connect_task", start)
        body = source[start:end]
        self.assertNotIn("reason=str(exc)", body)
        self.assertEqual(body.count("describe_error(exc)"), 2)



# ---------------------------------------------------------------------------
# v4.0.0 WP-2 / WP-4 / WP-7 regression tests (appended; WP-1 above is unchanged)
# ---------------------------------------------------------------------------

import json
import tempfile

from fastapi import HTTPException

from mlxbar.api import management as management_module
from mlxbar.api import openai_compat as openai_compat_module


class _Database:
    """Minimal metadata store for the gate tests."""

    def __init__(self, suspended="0", models=None):
        self.meta = {"api_autoload_suspended": suspended}
        self._models = models if models is not None else [
            {"id": "model-1", "name": "model-1", "engine": "mlx-lm"}]

    def metadata_value(self, key):
        return self.meta.get(key)

    def set_metadata_value(self, key, value):
        self.meta[key] = value

    def list_models(self):
        return list(self._models)


class _Workers:
    """Scripted pool: each load() call pops the next outcome."""

    def __init__(self, outcomes=None, loaded_ids=()):
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self._loaded = [{"id": model_id} for model_id in loaded_ids]

    async def load(self, model, pin=True, **kwargs):
        # v5.1.0: the preload passes `origin=` so the pool knows the load
        # is an explicit one; this fake only counts calls.
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return {"ok": True}

    def status(self):
        return {"loadedModels": list(self._loaded)}

    async def unload(self):
        raise NotImplementedError


def _app(*, database=None, workers=None, profiles=None, settings_data=None):
    data = settings_data if settings_data is not None else {
        "models": {"pool": {"profiles": profiles or []}},
        "general": {"preloadLastModel": False},
    }
    state = SimpleNamespace(
        settings=SimpleNamespace(data=data),
        database=database or _Database(),
        workers=workers or _Workers(),
        model_autoload_lock=asyncio.Lock(),
        preload_state={"state": "pending", "modelId": None, "reason": None,
                       "code": None, "at": None},
    )

    def set_preload_state(value, *, model_id=None, reason=None, code=None):
        state.preload_state = {"state": value, "modelId": model_id,
                               "reason": reason, "code": code, "at": 0.0}

    state.set_preload_state = set_preload_state
    return state


class PreloadTruthTests(unittest.TestCase):
    """WP-2a/b: a failed record is history; the live pool decides "loaded"."""

    def _failed_app(self, loaded_ids):
        app = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                   workers=_Workers(loaded_ids=loaded_ids))
        app.preload_state = {"state": "failed", "modelId": "model-1",
                             "reason": "timeout", "code": "MODEL_LOAD_TIMEOUT", "at": 1.0}
        return app

    def test_failed_then_loaded_reports_loaded_and_keeps_history(self):
        app = self._failed_app(loaded_ids=["model-1"])
        result = management_module._preload_status(
            app, worker_status=app.workers.status())
        self.assertEqual(result["state"], "loaded")
        # Existing keys stay, and the history of the failed attempt is kept.
        self.assertEqual(set(result), {"state", "modelId", "reason", "code", "at"})
        self.assertEqual(result["code"], "MODEL_LOAD_TIMEOUT")
        self.assertEqual(result["reason"], "timeout")

    def test_failed_and_still_not_loaded_stays_failed(self):
        app = self._failed_app(loaded_ids=[])
        result = management_module._preload_status(
            app, worker_status=app.workers.status())
        self.assertEqual(result["state"], "failed")

    def test_status_without_worker_snapshot_never_raises(self):
        app = self._failed_app(loaded_ids=["model-1"])
        # No worker_status supplied: must fall back to "failed", not raise.
        self.assertEqual(management_module._preload_status(app)["state"], "failed")


class _ExplodingUnloadWorkers:
    async def unload(self):
        raise self.error


class UnloadGateTests(unittest.IsolatedAsyncioTestCase):
    """WP-2c: the gate arms only after a successful unload."""

    async def _gate_after_unload(self, error):
        workers = _ExplodingUnloadWorkers()
        workers.error = error
        database = _Database(suspended="0")
        app = _app(database=database, workers=workers)
        original = management_module.state
        management_module.state = lambda request: app
        try:
            with contextlib.suppress(Exception):
                await management_module.unload(SimpleNamespace(), force=True)
        finally:
            management_module.state = original
        return database.metadata_value("api_autoload_suspended")

    async def test_unload_error_does_not_arm_the_gate(self):
        from mlxbar.errors import MLXBarError as Err
        gate = await self._gate_after_unload(Err("MODEL_BUSY", "busy", 409, False))
        self.assertEqual(gate, "0")

    async def test_plain_unload_exception_does_not_arm_the_gate(self):
        gate = await self._gate_after_unload(RuntimeError("boom"))
        self.assertEqual(gate, "0")


class _RetryWorkers:
    """Fails with the scripted errors, then succeeds."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def load(self, model, pin=True, **kwargs):
        # v5.1.0: the preload passes `origin=` so the pool knows the load
        # is an explicit one; this fake only counts calls.
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return {"ok": True}


class ResidentRetryTests(unittest.IsolatedAsyncioTestCase):
    """WP-2d: bounded automatic retry of transient resident failures."""

    def _transient(self):
        return MLXBarError("MODEL_LOAD_TIMEOUT", "時間切れ", 503, True)

    async def test_retryable_failure_then_success_ends_loaded(self):
        from mlxbar import main as main_module
        workers = _RetryWorkers([self._transient(), None])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        task = main_module._preload_last_model(state, retry_interval=0, retry_attempts=2)
        await task
        self.assertEqual(workers.calls, 2)
        self.assertEqual(state.preload_state["state"], "loaded")

    async def test_the_retry_yields_to_preload_task_cancellation(self):
        """WP-2(d) shutdown safety: the retry must not outlive the coordinator.

        `main.serve` cancels the preload task in its `finally`. The retry pass
        sleeps *inside* that task, so the cancellation has to reach it: a retry
        loop that ignored it would keep a process alive after SIGTERM (the
        "app that never quits" failure). The interval here is 30 s, so a loop
        that swallowed the cancellation would fail the 5 s wait below instead of
        quietly taking 30 s.
        """
        from mlxbar import main as main_module
        workers = _RetryWorkers([self._transient(), None])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        task = main_module._preload_last_model(state, retry_interval=30, retry_attempts=3)
        # Wait until the first pass failed and the task is parked in the sleep.
        for _ in range(200):
            await asyncio.sleep(0)
            if workers.calls == 1 and state.preload_state["state"] == "failed":
                break
        self.assertEqual(workers.calls, 1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        self.assertTrue(task.cancelled())
        # The retry never issued a second load after the cancel landed.
        self.assertEqual(workers.calls, 1)

    async def test_retry_stops_at_the_attempt_limit(self):
        from mlxbar import main as main_module
        workers = _RetryWorkers([self._transient(), self._transient(), self._transient()])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        await main_module._preload_last_model(state, retry_interval=0, retry_attempts=2)
        # 1 first attempt + 2 retries, then it gives up and stays failed.
        self.assertEqual(workers.calls, 3)
        self.assertEqual(state.preload_state["state"], "failed")

    async def test_resident_missing_is_not_retried(self):
        from mlxbar import main as main_module
        workers = _RetryWorkers([])
        state = _app(profiles=[{"modelId": "ghost", "keepLoaded": True}],
                     workers=workers)
        await main_module._preload_last_model(state, retry_interval=0, retry_attempts=3)
        self.assertEqual(workers.calls, 0)
        self.assertEqual(state.preload_state["code"], "RESIDENT_MODEL_MISSING")

    async def test_non_retryable_failure_is_not_retried(self):
        from mlxbar import main as main_module
        workers = _RetryWorkers([MLXBarError("MODEL_BROKEN", "壊れている", 422, False)])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        await main_module._preload_last_model(state, retry_interval=0, retry_attempts=3)
        self.assertEqual(workers.calls, 1)
        self.assertEqual(state.preload_state["state"], "failed")

    async def test_unpinned_model_is_not_retried(self):
        from mlxbar import main as main_module
        workers = _RetryWorkers([self._transient(), None])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        # Unpin during the first attempt's failure window: the retry must drop it.
        original = workers.load

        async def load_then_unpin(model, pin=True, **kwargs):
            try:
                return await original(model, pin=pin, **kwargs)
            finally:
                state.settings.data["models"]["pool"]["profiles"] = []
        workers.load = load_then_unpin
        await main_module._preload_last_model(state, retry_interval=0, retry_attempts=3)
        self.assertEqual(workers.calls, 1)

    async def test_retry_does_not_hold_the_autoload_lock_while_sleeping(self):
        from mlxbar import main as main_module
        held_during_sleep = []
        real_sleep = asyncio.sleep

        async def spy_sleep(delay, *a, **kw):
            held_during_sleep.append(state.model_autoload_lock.locked())
            await real_sleep(0)
        workers = _RetryWorkers([self._transient(), None])
        state = _app(profiles=[{"modelId": "model-1", "keepLoaded": True}],
                     workers=workers)
        original_sleep = main_module.asyncio.sleep
        main_module.asyncio.sleep = spy_sleep
        try:
            await main_module._preload_last_model(state, retry_interval=0, retry_attempts=2)
        finally:
            main_module.asyncio.sleep = original_sleep
        self.assertTrue(held_during_sleep, "retry pass must have slept at least once")
        self.assertFalse(any(held_during_sleep),
                         "model_autoload_lock must be free during the retry sleep")


class SettingsWriteTests(unittest.IsolatedAsyncioTestCase):
    """WP-4 / WP-7 against a real SettingsStore in a temp directory."""

    def _store(self, **kwargs):
        from mlxbar.settings import SettingsStore
        root = Path(tempfile.mkdtemp(prefix="mlxbar-test-"))
        return SettingsStore(root=root, **kwargs)

    async def test_a_newer_schema_file_is_refused_by_the_api_not_rewritten(self):
        """FIND-08: a v1 build must not save over a document a newer build owns.

        The refusal belongs to the HTTP layer, not `SettingsStore.update()`:
        v3.3.0 / AC-COMPAT-1 (pinned by `test_v3_3_0_exec_contracts`) requires the
        library's downgrade path to keep writing a v1-shaped document with the
        newer build's unknown keys carried through. So the API declines with 409
        SETTINGS_READ_ONLY and the file is left byte-identical.
        """
        from mlxbar.settings import SettingsStore
        store = self._store()
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["schemaVersion"] = 99
        store.path.write_text(json.dumps(raw), encoding="utf-8")
        before = store.path.read_text(encoding="utf-8")
        newer = SettingsStore(root=store.root)
        self.assertTrue(newer.forward_compatible)

        app = _app(database=_Database())
        app.settings = newer
        with self.assertRaises(HTTPException) as ctx:
            await management_module.put_settings(
                SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mlxbar=app))),
                {"general": {"preloadLastModel": False}})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "SETTINGS_READ_ONLY")
        self.assertEqual(store.path.read_text(encoding="utf-8"), before)

    def test_successful_update_bumps_the_revision(self):
        store = self._store()
        start = store.revision
        store.update({"general": {"preloadLastModel": False}})
        self.assertEqual(store.revision, start + 1)

    async def test_listener_failure_keeps_unrelated_concurrent_changes(self):
        """FIND-07: a failed PUT must not erase a change made in the same window."""
        store = self._store()
        old_port = store.data["api"]["port"]

        class FailingListener:
            async def switch(self, host, port):
                raise RuntimeError("bind failed")

        class SlowWriteListener(FailingListener):
            # While the listener is switching, an unrelated key lands in the store.
            async def switch(self, host, port):
                store.update({"general": {"preloadLastModel": False}})
                raise RuntimeError("bind failed")

        app = _app(settings_data=store.data)
        app.settings = store
        app.listener = SlowWriteListener()
        app.public_listener_error = None
        body = {"api": {"port": old_port + 1}}

        original = management_module.state
        management_module.state = lambda req: app
        try:
            with self.assertRaises(management_module.HTTPException) as ctx:
                await management_module.put_settings(SimpleNamespace(), body)
            self.assertEqual(ctx.exception.status_code, 409)
        finally:
            management_module.state = original
        # The listener change is reverted, the concurrent unrelated change survives.
        self.assertEqual(store.data["api"]["port"], old_port)
        self.assertEqual(store.data["general"]["preloadLastModel"], False)

    async def test_settings_write_times_out_instead_of_waiting_forever(self):
        store = self._store()
        app = _app(settings_data=store.data)
        app.settings = store
        await store.write_lock.acquire()  # simulate a stuck writer
        original = management_module.SETTINGS_LOCK_TIMEOUT_SECONDS
        management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(MLXBarError) as ctx:
                async with management_module._settings_write_guard(app):
                    pass
            self.assertEqual(ctx.exception.code, "SETTINGS_CONFLICT")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = original
            store.write_lock.release()

    async def test_keep_loaded_write_clears_the_gate_on_success(self):
        store = self._store()
        database = _Database(suspended="1")
        app = _app(settings_data=store.data, database=database)
        app.settings = store
        body = {"models": {"pool": {"profiles": [
            {"modelId": "model-1", "keepLoaded": True}]}}}
        original = management_module.state
        management_module.state = lambda req: app
        try:
            await management_module.put_settings(SimpleNamespace(), body)
        finally:
            management_module.state = original
        self.assertEqual(database.metadata_value("api_autoload_suspended"), "0")

    async def test_rejected_write_restores_the_gate(self):
        store = self._store()
        database = _Database(suspended="1")
        app = _app(settings_data=store.data, database=database)
        app.settings = store
        # keepLoaded is valid, but the port is out of range -> 422, gate must return to "1".
        body = {"models": {"pool": {"profiles": [
                    {"modelId": "model-1", "keepLoaded": True}]}},
                "api": {"port": 99999999}}
        original = management_module.state
        management_module.state = lambda req: app
        try:
            with self.assertRaises(management_module.HTTPException) as ctx:
                await management_module.put_settings(SimpleNamespace(), body)
            self.assertEqual(ctx.exception.status_code, 422)
        finally:
            management_module.state = original
        self.assertEqual(database.metadata_value("api_autoload_suspended"), "1")




class ProfileLockTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Review fix: a lock-wait timeout on the profile endpoint is a 409, not UnboundLocalError."""

    async def test_profile_write_timeout_returns_409_not_unbound_local(self):
        from mlxbar.settings import SettingsStore
        root = Path(tempfile.mkdtemp(prefix="mlxbar-test-"))
        store = SettingsStore(root=root)
        database = _Database(suspended="1")
        app = _app(settings_data=store.data, database=database)
        app.settings = store
        await store.write_lock.acquire()  # simulate a stuck writer holding the lock
        original_state = management_module.state
        original_timeout = management_module.SETTINGS_LOCK_TIMEOUT_SECONDS
        management_module.state = lambda req: app
        management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(management_module.HTTPException) as ctx:
                await management_module.put_model_profile(
                    "model-1", SimpleNamespace(),
                    {"keepLoaded": True})
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["code"], "SETTINGS_CONFLICT")
        finally:
            management_module.state = original_state
            management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = original_timeout
            store.write_lock.release()
        # The gate must be untouched: the write never happened.
        self.assertEqual(database.metadata_value("api_autoload_suspended"), "1")

class _HealthWorkers(_Workers):
    """Adds the `loaded_models()` reader `/v1/health` prefers."""

    def loaded_models(self):
        return list(self._loaded)


class HealthPreloadConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """WP-2a: `/v1/health` must not report a stale failure that `/status` fixed.

    Before this, the GUI-facing `/api/v1/status` derived "loaded" while the
    unauthenticated `/v1/health` echoed the raw record, so the same moment had
    two different answers depending on which endpoint you asked.
    """

    def _recovered_app(self):
        app = _app(
            profiles=[{"modelId": "model-1", "keepLoaded": True}],
            workers=_HealthWorkers(loaded_ids=["model-1"]),
            settings_data={
                "models": {"pool": {"profiles": [
                    {"modelId": "model-1", "keepLoaded": True}]}},
                "general": {"preloadLastModel": False},
                "api": {"requireToken": True},
            })
        app.preload_state = {"state": "failed", "modelId": "model-1",
                             "reason": "timeout", "code": "MODEL_LOAD_TIMEOUT", "at": 1.0}
        app.started_at = 0.0
        return app

    async def test_health_and_status_agree_after_the_resident_model_loads(self):
        app = self._recovered_app()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mlxbar=app)))
        payload = await openai_compat_module.health(request)
        self.assertEqual(payload["preload_state"], "loaded")
        status_payload = management_module._preload_status(
            app, worker_status=app.workers.status())
        self.assertEqual(status_payload["state"], "loaded")

    async def test_health_still_reports_failure_when_the_model_is_not_loaded(self):
        app = self._recovered_app()
        app.workers._loaded = []
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mlxbar=app)))
        payload = await openai_compat_module.health(request)
        self.assertEqual(payload["preload_state"], "failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
