"""v2.10.1: regression tests for the v2.10.0 post-release design review.

Every test here pins one audit finding that the patch release fixes:

* the pool honours an explicit ``mlx-lm``/``mlx-vlm`` choice on an
  already-resident model (v2.10.0's slot fast-path silently returned the
  other engine's ready slot, so the GUI engine menu did nothing for loaded
  models) while auto loads keep the no-churn fast path,
* the post-load catalog self-heal runs off the event loop (``to_thread``);
  the method itself stays synchronous,
* ``supervisor.load`` derives an engine for catalog rows classified ``None``
  and names the engine actually being tried during the symmetric fallback,
* ``_ensure_omlx_background`` arms the cached-status loop when oMLX is
  enabled at runtime (memory-budget under-count fix),
* the oMLX auto-connect task keeps its promised retry shape on raised
  (not folded) errors,
* and the subprocess runners (oMLX setup + runtime updater) tear the process
  group down when the job plumbing itself raises — not only on timeout or
  user cancellation.

Everything runs on fake model directories and short-lived /bin/sleep
children — no torch, no mlx, no real Homebrew, no real server.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from mlxbar.catalog import classifier as classifier_module  # noqa: E402
from mlxbar.catalog.classifier import classify  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.omlx_setup import OMLXSetupService  # noqa: E402
from mlxbar.runtimes.updater import RuntimeUpdater  # noqa: E402
from mlxbar.workers import model_pool as model_pool_module  # noqa: E402
from mlxbar.workers.model_pool import ModelPoolSupervisor  # noqa: E402
from mlxbar.workers.supervisor import WorkerSupervisor  # noqa: E402
import mlxbar.main as main_module  # noqa: E402

TEXT_CONFIG = {"model_type": "llama", "architectures": ["LlamaForCausalLM"]}
QWEN_VL_CONFIG = {
    "model_type": "qwen2_vl",
    "architectures": ["Qwen2VLForConditionalGeneration"],
}


def make_model_dir(base: Path, name: str, config: dict) -> Path:
    path = base / name
    path.mkdir(parents=True)
    (path / "config.json").write_text(__import__("json").dumps(config), encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 16)
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    return path


def db_row(model_id: str, path: Path, engine: str = "mlx-lm") -> dict:
    return {"id": model_id, "source": "local", "name": model_id, "path": str(path),
            "provider_key": None, "format": "mlx_lm", "engine": engine,
            "modalities": ["text"], "confidence": 0.8, "reason": "previous scan",
            "size_bytes": 1024}


def pool_settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(data={
        "generation": {"loadTimeoutSeconds": 5},
        "models": {
            "pool": {"enabled": True, "maxResidentModels": 2},
            "omlx": {"enabled": False, "serverUrl": "http://127.0.0.1:8000",
                     "apiKey": None, "autoConnect": False, "modelRouting": {}},
        },
    })


# --- explicit engine switch on a ready slot ----------------------------------

class ExplicitEngineSwitchTests(unittest.TestCase):
    """`load(model, "mlx-vlm")` on a model already resident on mlx-lm must
    actually reload it instead of returning the old slot (v2.10.1)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pool(self, engine="mlx-lm"):
        db = Database(self.tmp / "state.sqlite3")
        db.replace_models([db_row("m1", self.text_dir, engine)])
        pool = ModelPoolSupervisor(self.tmp, pool_settings(), db)
        self.calls: list[tuple[str | None, bool]] = []
        self.load_patch = patch.object(model_pool_module.SingleWorkerSupervisor,
                                       "load", self.make_fake_load())
        self.call_patch = patch.object(model_pool_module.SingleWorkerSupervisor,
                                       "_call", self.make_fake_call())
        self.load_patch.start()
        self.call_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.call_patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        return db, pool

    def make_fake_load(self):
        async def fake_load(self_worker, model, engine=None, explicit=None):
            self.calls.append((engine, bool(explicit)))
            loaded = {"id": model.get("id"), "engine": engine or "mlx-lm",
                      "state": "loaded",
                      "capabilities": {"memoryLimits": {
                          "set_memory_limit": self_worker.memory_limit_bytes}}}
            self_worker.loaded = loaded
            return loaded
        return fake_load

    def make_fake_call(self):
        async def fake_call(self_worker, method, params, timeout=None):
            return {"memory": {}}
        return fake_call

    def model(self, engine="mlx-lm"):
        return {"id": "m1", "name": "M", "path": str(self.text_dir),
                "engine": engine, "size_bytes": 1024}

    def test_explicit_choice_switches_ready_slot(self):
        _, pool = self.make_pool()
        first = asyncio.run(pool.load(self.model(), None, pin=True))
        self.assertEqual(first["engine"], "mlx-lm")
        self.assertEqual(len(pool._slots), 1)

        second = asyncio.run(pool.load(self.model(), "mlx-vlm", pin=True))
        self.assertEqual(second["engine"], "mlx-vlm")
        # Exactly two worker loads happened: the original auto load and the
        # explicit switch. The second one arrived with the chosen engine.
        self.assertEqual(self.calls, [(None, False), ("mlx-vlm", True)])
        # One ready slot remains, now serving the requested engine.
        self.assertEqual(len(pool._slots), 1)
        for slot in pool._slots.values():
            self.assertEqual(slot.state, "ready")
            self.assertEqual((slot.worker.loaded or {}).get("engine"), "mlx-vlm")

    def test_auto_request_keeps_ready_slot(self):
        """The v2.10.0 fast path is intact for auto loads: no reload, no
        churn even when the catalog engine differs from the running one."""
        db, pool = self.make_pool()
        model = self.model()
        asyncio.run(pool.load(model, None, pin=True))
        del self.calls[:]
        # Simulate a slot that was re-derived to mlx-vlm while the catalog row
        # still says mlx-lm (exactly the self-heal scenario).
        for slot in pool._slots.values():
            slot.worker.loaded["engine"] = "mlx-vlm"
        result = asyncio.run(pool.load(model, None, pin=True))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(self.calls, [])

    def test_switch_refuses_while_leases_active(self):
        _, pool = self.make_pool()
        asyncio.run(pool.load(self.model(), None, pin=True))
        for slot in pool._slots.values():
            slot.leases = 1
        with self.assertRaises(MLXBarError) as raised:
            asyncio.run(pool.load(self.model(), "mlx-vlm", pin=True))
        self.assertEqual(raised.exception.code, "ENGINE_BUSY")
        for slot in pool._slots.values():
            self.assertEqual((slot.worker.loaded or {}).get("engine"), "mlx-lm")

    def test_switch_to_current_engine_is_noop(self):
        _, pool = self.make_pool()
        asyncio.run(pool.load(self.model(), None, pin=True))
        active = asyncio.run(pool.load(self.model(), "mlx-lm", pin=True))
        self.assertEqual(active["engine"], "mlx-lm")
        self.assertEqual(self.calls, [(None, False)])


# --- self-heal off the event loop --------------------------------------------

class SelfHealThreadTests(unittest.TestCase):
    """The classification write takes the catalog lock; a concurrent scan
    holds that same lock in an executor thread. The load chain must not run
    it inline on the event loop (v2.10.1)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_self_heal_runs_off_the_event_loop(self):
        db = Database(self.tmp / "state.sqlite3")
        db.replace_models([db_row("m1", self.vlm_dir, "mlx-lm")])
        pool = ModelPoolSupervisor(self.tmp, pool_settings(), db)
        threads: list[str] = []
        real_classify = classifier_module.classify

        def classify_spy(path):
            threads.append(threading.current_thread().name)
            return real_classify(path)

        async def fake_load(self_worker, model, engine=None, explicit=None):
            return {"id": model.get("id"), "engine": "mlx-vlm", "state": "loaded",
                    "capabilities": {"memoryLimits": {
                        "set_memory_limit": self_worker.memory_limit_bytes}}}

        async def fake_call(self_worker, method, params, timeout=None):
            return {"memory": {}}

        with patch.object(model_pool_module, "_classify_catalog_model",
                          side_effect=classify_spy), \
             patch.object(model_pool_module.SingleWorkerSupervisor, "load", fake_load), \
             patch.object(model_pool_module.SingleWorkerSupervisor, "_call", fake_call):
            result = asyncio.run(pool.load(
                {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-lm", "size_bytes": 1024}, None, pin=True))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertTrue(threads, "self-heal classification never ran")
        self.assertNotEqual(threads[0], "MainThread")

    def test_self_heal_failure_still_never_surfaces(self):
        db = Database(self.tmp / "state.sqlite3")
        db.replace_models([db_row("m1", self.vlm_dir, "mlx-lm")])
        pool = ModelPoolSupervisor(self.tmp, pool_settings(), db)

        async def fake_load(self_worker, model, engine=None, explicit=None):
            return {"id": model.get("id"), "engine": "mlx-vlm", "state": "loaded",
                    "capabilities": {"memoryLimits": {
                        "set_memory_limit": self_worker.memory_limit_bytes}}}

        async def fake_call(self_worker, method, params, timeout=None):
            return {"memory": {}}

        with patch.object(model_pool_module, "_classify_catalog_model",
                          side_effect=OSError("disk gone")), \
             patch.object(model_pool_module.SingleWorkerSupervisor, "load", fake_load), \
             patch.object(model_pool_module.SingleWorkerSupervisor, "_call", fake_call):
            result = asyncio.run(pool.load(
                {"id": "m1", "name": "M", "path": str(self.vlm_dir),
                 "engine": "mlx-lm", "size_bytes": 1024}, None, pin=True))
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(db.get_model("m1")["engine"], "mlx-lm")


# --- supervisor: derivation for None rows + fallback wording ------------------

class SupervisorDeriveAndPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sup = WorkerSupervisor(self.tmp, types.SimpleNamespace(data={
            "generation": {"loadTimeoutSeconds": 5},
            "models": {"lmStudio": {"baseUrl": "http://127.0.0.1:9999"}},
        }))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def instrument(self, fails: bool = True, phases: list | None = None):
        starts: list[str] = []

        async def fake_start(engine: str) -> None:
            if phases is not None:
                # The phase value at spawn time is what the menu bar renders
                # during the handoff ("Workerを起動中" / "<engine>へ切り替え中").
                phases.append(self.sup.loading["phase"])
            starts.append(engine)

        async def fake_call(method: str, params: dict, timeout=None) -> dict:
            if fails:
                raise MLXBarError("MODEL_INCOMPATIBLE", "runtime load failed", 400)
            return {"type": "completed", "capabilities": {}}

        self.sup._start_worker = fake_start  # type: ignore[method-assign]
        self.sup._call = fake_call  # type: ignore[method-assign]
        return starts

    def test_auto_derives_engine_for_none_catalog_row(self):
        """A row classified `None` no longer refuses auto loads with a generic
        409 — auto means derive from the directory when it is readable."""
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts = self.instrument(fails=False)
        loaded = asyncio.run(self.sup.load(
            {"id": "mA", "name": "T", "path": str(text_dir)}, None))
        self.assertEqual(loaded["engine"], "mlx-lm")
        self.assertEqual(starts, ["mlx-lm"])

    def test_none_catalog_row_with_unreadable_path_still_refuses(self):
        starts = self.instrument(fails=False)
        with self.assertRaises(MLXBarError) as raised:
            asyncio.run(self.sup.load({"id": "mB", "name": "T",
                                       "path": str(self.tmp)}, None))
        self.assertIn("利用可能なエンジン", str(raised.exception))
        self.assertEqual(starts, [])

    def test_explicit_none_stays_strict(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        starts = self.instrument(fails=False)
        asyncio.run(self.sup.load(
            {"id": "mC", "name": "T", "path": str(text_dir), "engine": "mlx-lm"},
            "mlx-lm"))
        self.assertEqual(starts, ["mlx-lm"])

    def test_fallback_phase_names_the_engine(self):
        """After the symmetric fallback, switching mlx-vlm -> mlx-lm must not
        claim "mlx-vlm へ切り替え中" for the *next* engine (v2.10.1)."""
        vlm_dir = make_model_dir(self.tmp, "vl", QWEN_VL_CONFIG)
        phases: list[str] = []
        self.instrument(fails=True, phases=phases)
        with self.assertRaises(MLXBarError):
            asyncio.run(self.sup.load(
                {"id": "m1", "name": "M", "path": str(vlm_dir), "engine": "mlx-lm"},
                None))
        self.assertEqual(phases, ["Workerを起動中", "mlx-lmへ切り替え中"])

    def test_reverse_fallback_phase_names_mlx_vlm(self):
        text_dir = make_model_dir(self.tmp, "text", TEXT_CONFIG)
        phases: list[str] = []
        self.instrument(fails=True, phases=phases)
        with self.assertRaises(MLXBarError):
            asyncio.run(self.sup.load(
                {"id": "m2", "name": "M", "path": str(text_dir), "engine": "mlx-vlm"},
                None))
        self.assertEqual(phases, ["Workerを起動中", "mlx-vlmへ切り替え中"])


# --- oMLX background status loop ----------------------------------------------

class EnsureOmlxBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_pool(self):
        settings = pool_settings()
        pool = ModelPoolSupervisor(self.tmp, settings, None)
        self.settings = settings
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        return pool

    def test_starts_loop_when_enabled_at_runtime(self):
        async def run():
            pool = self.make_pool()
            try:
                self.assertFalse(pool._omlx_enabled())
                self.assertIsNone(pool._omlx_provider._status_update_task)
                self.settings.data["models"]["omlx"]["enabled"] = True
                pool._ensure_omlx_background()
                first = pool._omlx_provider._status_update_task
                self.assertIsNotNone(first)
                # Idempotent: a second call must not spawn a competing loop.
                pool._ensure_omlx_background()
                self.assertIs(pool._omlx_provider._status_update_task, first)
            finally:
                await pool._omlx_provider.close()
        asyncio.run(run())

    def test_disabled_keeps_loop_off(self):
        async def run():
            pool = self.make_pool()
            self.assertFalse(pool._omlx_enabled())
            pool._ensure_omlx_background()
            self.assertIsNone(pool._omlx_provider._status_update_task)
        asyncio.run(run())

    def test_cached_accounting_sees_the_loop(self):
        """_get_omlx_memory_usage_cached arms the loop too — the resident
        charge path is the consumer that under-counted."""
        async def run():
            pool = self.make_pool()
            try:
                self.assertIsNone(pool._omlx_provider._status_update_task)
                self.settings.data["models"]["omlx"]["enabled"] = True
                self.assertEqual(pool._get_omlx_memory_usage_cached(), 0)
                self.assertIsNotNone(pool._omlx_provider._status_update_task)
            finally:
                await pool._omlx_provider.close()
        asyncio.run(run())


# --- oMLX auto-connect retry shape --------------------------------------------

class AutoConnectRetryTests(unittest.TestCase):
    def make_state(self, connect):
        return types.SimpleNamespace(
            settings=types.SimpleNamespace(data={"models": {
                "omlx": {"enabled": True, "autoConnect": True}}}),
            workers=types.SimpleNamespace(_omlx_provider=object(),
                                          connect_omlx=connect))

    def test_raised_errors_burn_one_attempt_each(self):
        attempts: list[int] = []

        async def connect():
            attempts.append(len(attempts))
            raise RuntimeError("transient coordinator hiccup")

        state = self.make_state(connect)

        async def run():
            task = main_module._omlx_auto_connect_task(state, max_attempts=3,
                                                       interval=0.001)
            self.assertIsNotNone(task)
            await task  # never raises; every failure is foldable bookkeeping

        asyncio.run(run())
        self.assertEqual(len(attempts), 3)

    def test_success_stops_the_loop(self):
        calls: list[int] = []

        async def connect():
            calls.append(1)
            return {"connected": True, "error": None, "serverUrl": "http://x"}

        state = self.make_state(connect)

        async def run():
            task = main_module._omlx_auto_connect_task(state, max_attempts=6,
                                                       interval=0.001)
            await task

        asyncio.run(run())
        self.assertEqual(len(calls), 1)

    def test_not_reachable_still_honours_the_budget(self):
        calls: list[int] = []

        async def connect():
            calls.append(1)
            return {"connected": False, "error": "connection refused",
                    "serverUrl": "http://x"}

        state = self.make_state(connect)

        async def run():
            task = main_module._omlx_auto_connect_task(state, max_attempts=4,
                                                       interval=0.001)
            await task

        asyncio.run(run())
        self.assertEqual(len(calls), 4)

    def test_disabled_returns_without_a_task(self):
        async def connect():  # pragma: no cover - must never run
            raise AssertionError("connect called for a disabled setting")

        state = self.make_state(connect)
        state.settings.data["models"]["omlx"]["autoConnect"] = False
        self.assertIsNone(main_module._omlx_auto_connect_task(state))


# --- subprocess runners survive plumbing explosions --------------------------

class CommandRunnerTearDownTests(unittest.TestCase):
    """A raise from the *job plumbing* (update/heartbeat writers) must kill
    the child the same way timeouts and user cancellation do (v2.10.1)."""

    def run_with_exploding_callback(self, make_call):
        """Run `make_call(exploding)` — exploding raises on its first read.
        The child must be gone (returncode set) when the raise lands."""
        async def run():
            procs: dict = {}
            real_exec = asyncio.create_subprocess_exec

            async def spy_exec(*args, **kwargs):
                proc = await real_exec(*args, **kwargs)
                procs["proc"] = proc
                return proc

            async def exploding(*_args, **_kwargs):
                raise RuntimeError("plumbing exploded")

            with patch("asyncio.create_subprocess_exec", spy_exec):
                with self.assertRaises(RuntimeError):
                    await make_call(exploding)
            return procs["proc"].returncode

        return asyncio.run(run())

    def test_omlx_setup_runner_kills_child(self):
        service = OMLXSetupService()

        async def noop(progress, message):
            return None

        def make_call(exploding):
            # The setup runner drives progress through `heartbeat`; the job
            # `update` stays a no-op here, mirroring the real wiring.
            return service._run_command(["/bin/sleep", "300"], noop,
                                        timeout=120, heartbeat=exploding)

        self.assertIsNotNone(self.run_with_exploding_callback(make_call))

    def test_runtime_updater_runner_kills_child(self):
        updater = RuntimeUpdater(store=None)

        def make_call(exploding):
            return updater._command("/bin/sleep", "300", heartbeat=exploding,
                                    timeout=120)

        self.assertIsNotNone(self.run_with_exploding_callback(make_call))

    def test_omlx_setup_timeout_path_still_translates(self):
        service = OMLXSetupService()

        async def run():
            async def update(progress, message):
                return None

            with self.assertRaises(MLXBarError) as raised:
                await service._run_command(["/bin/sleep", "300"], update,
                                           timeout=1)
            return raised.exception.code

        self.assertEqual(asyncio.run(run()), "OMLX_COMMAND_TIMEOUT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
