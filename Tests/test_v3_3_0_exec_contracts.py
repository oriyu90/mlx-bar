"""v3.2.3 / v3.3.0 execution contracts (TASK-004).

One test per acceptance criterion that the implementation can prove without a
GPU, a model, or a running app. The measurements behind these numbers are in
`tasks/TASK-004/EXEC/TEST_RESULTS.md`.

Deliberately narrow: they pin the *new* safety properties, not every existing
behaviour (the v2.x suites already do that).
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxbar.settings import DEFAULTS, SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor
from mlxbar.errors import MLXBarError

REPO = Path(__file__).resolve().parents[1]

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
from test_model_pool import FakeWorker  # noqa: E402


def _lookup(document: dict, path: str):
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


class SettingsContractTests(unittest.TestCase):
    def test_swift_config_keys_exist_in_the_python_schema(self):
        """AC-SET-1 (one direction): Swift writes only keys the schema defines.

        The GUI reaching a key Python does not know is exactly the v3.2.0
        failure this task is closing (`bundledLoaders` was unreachable rather
        than unknown, but the same mismatch hides both ways).
        """
        sources = REPO / "Sources" / "MLXBar"
        if not sources.exists():  # packaged install / partial checkout
            self.skipTest("Swift sources not present")
        references = set()
        for path in sources.rglob("*.swift"):
            text = path.read_text(errors="ignore")
            for match in re.finditer(r'(?:setConfig|nestedPatch)\(\s*"([A-Za-z0-9_.]+)"',
                                     text):
                references.add(match.group(1))
        self.assertGreater(len(references), 10, "key scan found almost nothing")
        unknown = sorted(key for key in references if not _lookup(DEFAULTS, key)[0])
        self.assertEqual(unknown, [], f"Swift writes keys missing from settings.DEFAULTS: {unknown}")

    def test_unknown_key_is_rejected_and_not_persisted(self):
        """AC-SET-4: a typo is a 422, not a permanent silent key."""
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            with self.assertRaisesRegex(ValueError, "unknown setting"):
                store.update({"models": {"pool": {"maxResidentModel": 3}}})
            self.assertNotIn("maxResidentModel", store.data["models"]["pool"])
            self.assertNotIn("maxResidentModel",
                             json.loads((Path(directory) / "config.json").read_text())["models"]["pool"])

    def test_newer_schema_is_read_without_rewriting_the_file(self):
        """AC-COMPAT-1: a downgrade must not truncate a newer config."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SettingsStore(root)
            config = root / "config.json"
            document = json.loads(config.read_text())
            document["schemaVersion"] = 2
            document["api"]["port"] = 12001
            document["futureSection"] = {"answer": 42}
            config.write_text(json.dumps(document))
            before = config.read_bytes()
            store = SettingsStore(root)
            self.assertTrue(store.forward_compatible)
            self.assertEqual(store.data["api"]["port"], 12001)
            self.assertNotIn("futureSection", store.data)
            self.assertIsNone(store.recovered_from)
            self.assertEqual(config.read_bytes(), before, "loading rewrote a newer config")
            # A later save from this (older) build must not delete it either.
            store.update({"api": {"port": 12002}})
            saved = json.loads(config.read_text())
            self.assertEqual(saved["api"]["port"], 12002)
            self.assertEqual(saved["futureSection"], {"answer": 42})
            # The document this build writes is v1-shaped, so it declares v1 --
            # claiming v2 while writing v1 keys would mislead the newer build
            # into skipping its own migration. The unknown keys it cannot
            # interpret are carried through verbatim (asserted above), which is
            # what "a downgrade must not lose settings" actually requires.
            self.assertEqual(saved["schemaVersion"], 1)

    def test_bad_values_fall_back_per_key(self):
        """AC-CS-2: one out-of-range key does not discard every setting."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.update({"generation": {"totalTimeoutSeconds": 1800}})
            document = json.loads((root / "config.json").read_text())
            document["generation"]["maxTokens"] = 99_999_999
            document["api"]["port"] = 80
            (root / "config.json").write_text(json.dumps(document))
            recovered = SettingsStore(root)
            self.assertEqual(sorted(recovered.recovered_keys),
                             ["api.port", "generation.maxTokens"])
            # The healthy neighbour of a broken key survives.
            self.assertEqual(recovered.data["generation"]["totalTimeoutSeconds"], 1800)
            self.assertIsNone(recovered.recovered_from)


class MemoryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, **pool) -> SettingsStore:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = SettingsStore(root)
        store.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 1, "idleTTLSeconds": 30, **pool})
        return store

    def _pool(self, store, capacity) -> ModelPoolSupervisor:
        pool = ModelPoolSupervisor(store.root, store)
        pool._physical_memory = lambda: 96 * GIB
        pool._host_capacity = capacity
        self.addAsyncCleanup(pool.shutdown)
        return pool

    async def test_unknown_weight_size_is_charged_conservatively(self):
        """AC-MS-1: size 0 means unmeasured, not free."""
        store = self._store()
        pool = self._pool(store, lambda: (80 * GIB, 0))
        estimate = pool._cold_estimate({"id": "m", "size_bytes": 0})
        self.assertGreaterEqual(estimate, 8 * GIB)
        self.assertGreater(estimate, 4 * GIB, "still treated as a small model")

    async def test_unmeasurable_host_refuses_the_load(self):
        """AC-MS-2: a failed probe disables the guard today; it must not."""
        store = self._store()
        pool = self._pool(store, lambda: (0, 0))
        with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker):
            with self.assertRaises(MLXBarError) as caught:
                await pool.load({"id": "m", "name": "m", "engine": "mlx-lm",
                                 "size_bytes": 1 * GIB}, pin=False)
        self.assertEqual(caught.exception.code, "MEMORY_HEADROOM_INSUFFICIENT")
        self.assertIn("計測不能", caught.exception.message)

    async def test_explicit_per_model_limit_is_honoured(self):
        """AC-SET-3: an explicit 32 GiB ceiling must reject a 33 GiB model."""
        store = self._store(defaultPerModelMaxGB=32, defaultPerModelMaxGBSet=True)
        pool = self._pool(store, lambda: (80 * GIB, 0))
        with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker):
            with self.assertRaises(MLXBarError) as caught:
                await pool.load({"id": "big", "name": "big", "engine": "mlx-lm",
                                 "size_bytes": 33 * GIB}, pin=False)
        self.assertEqual(caught.exception.code, "MODEL_MEMORY_LIMIT")

    async def test_crash_loop_breaker_and_explicit_release(self):
        """AC-CS-1: two deaths in 300s stop the third load, unload clears it."""
        store = self._store(defaultPerModelMaxGB=32)

        class DyingWorker(FakeWorker):
            async def load(self, model, engine, explicit=False):
                raise MLXBarError("WORKER_CRASHED", "boom", 503, True)

        pool = self._pool(store, lambda: (24 * GIB, 0))
        with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", DyingWorker):
            codes = []
            for _ in range(3):
                try:
                    await pool.load({"id": "m", "name": "m", "engine": "mlx-lm",
                                     "size_bytes": 1 * GIB}, pin=False)
                except MLXBarError as exc:
                    codes.append((exc.code, exc.retryable))
        self.assertEqual(codes[0][0], "WORKER_CRASHED")
        self.assertEqual(codes[2], ("MODEL_CRASH_LOOP", False))
        pool.clear_model_crash("m")
        self.assertFalse(pool.model_crash_loop("m"))

    async def test_replica_retry_backs_off_exponentially(self):
        """AC-ST-3: 30 -> 60 -> 120 ... capped at 600 seconds."""
        store = self._store(maxReplicasPerModel=4, maxResidentModels=3)
        pool = self._pool(store, lambda: (80 * GIB, 0))
        with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker):
            await pool.load({"id": "m", "name": "m", "engine": "mlx-lm",
                             "size_bytes": 2 * GIB}, pin=True)
            slot = next(iter(pool._slots.values()))
            slot.keep_loaded = True
            pool._desired_replicas = lambda model_id: 3

            async def refuse(*args, **kwargs):
                raise MLXBarError("MEMORY_HEADROOM_INSUFFICIENT", "no room", 503, True)

            pool._load_replica = refuse
            delays = []
            for _ in range(3):
                await pool._scale_up_pinned_replicas()
                state = pool._replica_retry["m"]
                delays.append(state["delaySeconds"])
                state["nextAt"] = 0  # pretend the timer elapsed
        self.assertEqual(delays, [30.0, 60.0, 120.0])


if __name__ == "__main__":
    unittest.main()


class NonBlockingStartupIoTests(unittest.TestCase):
    """v3.3.1: model metadata reads must stay off the event loop.

    The startup preload loads a resident model. Reading its directory
    (max-token probe, engine detection, capability classification) used to run
    synchronously inside the coroutine, so on an external or sleeping volume the
    whole coordinator stopped answering `/health` for as long as the disk took.
    The fix routes every one of those reads through ``asyncio.to_thread``; this
    test fails if any of them comes back.
    """

    def test_load_routes_directory_reads_through_to_thread(self):
        import asyncio
        import inspect

        from mlxbar.workers import supervisor as supervisor_module

        source = inspect.getsource(supervisor_module.WorkerSupervisor.load)
        # Each sync helper must be dispatched, never called directly.
        self.assertIn("asyncio.to_thread", source,
                      "load() must dispatch blocking I/O")
        for helper in ("_detect_model_max_tokens", "detect_engine",
                       "_catalog_capabilities"):
            if helper == "_detect_model_max_tokens":
                self.assertIn("asyncio.to_thread", source)
                self.assertNotIn(
                    "        model_max_tokens = self._detect_model_max_tokens",
                    source,
                    "the max-token probe must not run inline on the loop")
            if helper == "detect_engine":
                self.assertNotIn(
                    "            detected = detect_engine(Path(model[\"path\"]))",
                    source,
                    "engine detection must not run inline on the loop")
            if helper == "_catalog_capabilities":
                self.assertNotIn(
                    "                    capabilities, runtime_fit = _catalog_capabilities(model)",
                    source,
                    "capability classification must not run inline on the loop")
                self.assertNotIn(
                    "                advertised, _fit = _catalog_capabilities(model)",
                    source,
                    "capability classification must not run inline on the loop")

    def test_to_thread_dispatch_actually_yields(self):
        # A coroutine that blocks inside to_thread must not stall the loop.
        import asyncio
        import time

        async def scenario():
            ticks = 0
            done = False

            def slow_io():
                time.sleep(0.3)
                return {"ok": True}

            async def heartbeat():
                nonlocal ticks
                while not done:
                    ticks += 1
                    await asyncio.sleep(0.02)

            task = asyncio.create_task(heartbeat())
            result = await asyncio.to_thread(slow_io)
            done = True
            await task
            return result, ticks

        result, ticks = asyncio.run(scenario())
        self.assertTrue(result["ok"])
        self.assertGreater(ticks, 3, "the loop kept running during the blocking read")
