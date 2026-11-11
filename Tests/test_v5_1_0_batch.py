"""v5.1.0 continuous batching: scheduler, adapter fallbacks, pool lanes, settings.

These tests use a fake ``BatchGenerator`` that behaves like the real one on the
points this code depends on (per-sequence uids, a ``Response`` per active
sequence per step, ``finish_reason`` on the last token, ``remove`` for
cancellation, ``close`` for teardown). The real runtime is exercised separately
by ``scripts/verify-batch-runtime.py`` on an Apple Silicon machine.
"""
from __future__ import annotations

import asyncio
import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from common.batch import BatchRequest, BatchScheduler, BatchUnavailable  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor  # noqa: E402


class FakeGenerator:
    """Minimal stand-in for ``mlx_lm.generate.BatchGenerator``."""

    #: Default script for a prompt with no explicit one. The last token is the
    #: one that carries ``finish_reason`` and is deliberately NOT published
    #: (the runtime reports no text for it), so a 3-token script yields 2.
    DEFAULT_SCRIPT = [1, 2, 3]

    instances: list["FakeGenerator"] = []
    #: token ids each sequence emits, per prompt (by first prompt token)
    scripts: dict[int, list[int]] = {}
    #: raise from next() once, after this many steps
    fail_at_step: int | None = None
    #: delay per next() call, so concurrency is observable
    step_delay = 0.0

    def __init__(self, model, *, completion_batch_size=32, prefill_batch_size=8,
                 prefill_step_size=2048, **_):
        self.model = model
        self.completion_batch_size = completion_batch_size
        self.prefill_batch_size = prefill_batch_size
        self.prefill_step_size = prefill_step_size
        self.sequences: dict[int, dict] = {}
        self.removed: list[int] = []
        self.closed = False
        self.steps = 0
        self._next_uid = 0
        self.max_seen = 0
        FakeGenerator.instances.append(self)

    def insert(self, prompts, max_tokens=None, caches=None, all_tokens=None,
               samplers=None, logits_processors=None, stop_sequences=None):
        uids = []
        for index, prompt in enumerate(prompts):
            uid = self._next_uid
            self._next_uid += 1
            script = list(FakeGenerator.scripts.get(prompt[0], FakeGenerator.DEFAULT_SCRIPT))
            self.sequences[uid] = {
                "prompt": list(prompt),
                "max_tokens": (max_tokens or [128] * len(prompts))[index],
                "script": script,
                "emitted": 0,
                "sampler": (samplers or [None] * len(prompts))[index],
                "processors": (logits_processors or [None] * len(prompts))[index],
                "stop": (stop_sequences or [None] * len(prompts))[index],
            }
            uids.append(uid)
        self.max_seen = max(self.max_seen, len(self.sequences))
        return uids

    def next(self):
        if FakeGenerator.step_delay:
            time.sleep(FakeGenerator.step_delay)
        if FakeGenerator.fail_at_step is not None and self.steps == FakeGenerator.fail_at_step:
            FakeGenerator.fail_at_step = None
            raise RuntimeError("synthetic batch failure")
        self.steps += 1
        prompt_responses = []
        generation_responses = []
        for uid, sequence in list(self.sequences.items()):
            if sequence["emitted"] < len(sequence["script"]):
                token = sequence["script"][sequence["emitted"]]
                sequence["emitted"] += 1
                reason = None
                if sequence["emitted"] >= sequence["max_tokens"]:
                    reason = "length"
                elif sequence["emitted"] >= len(sequence["script"]):
                    reason = "stop"
                generation_responses.append(SimpleNamespace(
                    uid=uid, token=token, logprobs=None, finish_reason=reason,
                    prompt_cache=None, all_tokens=None))
                if reason is not None:
                    self.sequences.pop(uid, None)
        return prompt_responses, generation_responses

    def remove(self, uids, return_prompt_caches=False):
        for uid in uids:
            self.sequences.pop(uid, None)
            self.removed.append(uid)
        return {}

    def close(self):
        self.closed = True


class FakeTokenizer:
    def __init__(self, table=None):
        self.table = table or {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F"}
        self.eos_token_ids = [9]

    def encode(self, text, add_special_tokens=True):
        return [100 + (ord(char) % 50) for char in text]

    def decode(self, tokens):
        return "".join(self.table.get(token, "?") for token in tokens)


def drain(request: BatchRequest, timeout: float = 5.0) -> list[dict]:
    """Collect a request's events until it ends (or the deadline passes)."""
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = request.events.get(timeout=0.05)
        except queue.Empty:
            continue
        events.append(event)
        if event.get("type") in {"finished", "error"}:
            break
    return events


class SchedulerTests(unittest.TestCase):
    def make(self, **kwargs) -> BatchScheduler:
        # Per-test overrides are read (and then cleared) by `make`, so a test
        # can set them before calling it without leaking into the next one.
        scripts = FakeGenerator.scripts
        fail_at_step = FakeGenerator.fail_at_step
        step_delay = FakeGenerator.step_delay
        FakeGenerator.instances = []
        FakeGenerator.scripts = {}
        FakeGenerator.fail_at_step = None
        FakeGenerator.step_delay = 0.0
        scheduler = BatchScheduler(
            object(), max_sequences=kwargs.pop("max_sequences", 2),
            prefill_batch_size=kwargs.pop("prefill_batch_size", 2),
            prefill_step_size=kwargs.pop("prefill_step_size", 1024),
            make_prompt_cache=lambda model: [SimpleNamespace(merge=lambda *a: None)],
            batch_generator_factory=FakeGenerator,
        )
        self.assertTrue(scheduler.probe())
        FakeGenerator.scripts = scripts or {}
        FakeGenerator.fail_at_step = fail_at_step
        FakeGenerator.step_delay = step_delay
        scheduler.start()
        self.addCleanup(scheduler.shutdown)
        return scheduler

    def test_two_sequences_run_in_the_same_batch(self):
        FakeGenerator.scripts = {1: [1, 2, 3], 3: [1, 2, 3]}
        scheduler = self.make(max_sequences=2)
        first = BatchRequest("r1", [1, 2], 8)
        second = BatchRequest("r2", [3, 4], 8)
        scheduler.submit(first)
        scheduler.submit(second)
        self.assertEqual(drain(first)[-1]["type"], "finished")
        self.assertEqual(drain(second)[-1]["type"], "finished")
        self.assertEqual(FakeGenerator.instances[0].max_seen, 2)
        self.assertEqual(first.generated, [1, 2])
        self.assertEqual(second.generated, [1, 2])
        # The two sequences never share a uid, i.e. their KV caches are separate.
        self.assertNotEqual(first.uid, second.uid)

    def test_cancel_removes_only_that_sequence(self):
        FakeGenerator.scripts = {1: [1, 2, 3, 4, 5, 6], 3: [1, 2, 3, 4, 5, 6]}
        FakeGenerator.step_delay = 0.1
        scheduler = self.make(max_sequences=2)
        first = BatchRequest("r1", [1], 8)
        second = BatchRequest("r2", [3], 8)
        scheduler.submit(first)
        scheduler.submit(second)
        time.sleep(0.05)
        self.assertTrue(scheduler.cancel("r1"))
        events = drain(first)
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(events[-1]["finish_reason"], "cancelled")
        self.assertEqual(drain(second)[-1]["finish_reason"], "stop")
        self.assertIn(first.uid, FakeGenerator.instances[0].removed)

    def test_max_tokens_finishes_with_length(self):
        scheduler = self.make(max_sequences=2)
        request = BatchRequest("r1", [1], 2)
        scheduler.submit(request)
        events = drain(request)
        self.assertEqual(events[-1]["finish_reason"], "length")
        self.assertEqual(len(request.generated), 2)

    def test_runtime_error_fails_the_batch_and_never_retries(self):
        FakeGenerator.scripts = {1: [1, 2, 3, 4, 5], 3: [1, 2, 3, 4, 5]}
        FakeGenerator.fail_at_step = 1
        scheduler = self.make(max_sequences=2)
        first = BatchRequest("r1", [1], 8)
        second = BatchRequest("r2", [3], 8)
        scheduler.submit(first)
        scheduler.submit(second)
        for request in (first, second):
            events = drain(request)
            self.assertEqual(events[-1]["type"], "error")
            self.assertEqual(events[-1]["code"], "GENERATION_FAILED")
            # Exactly one step ran before the failure: nothing was restarted
            # behind the caller's back, and the token that did arrive is the
            # only one that exists.
            self.assertEqual(len(request.generated), 1)

    def test_capability_probe_refuses_caches_without_merge(self):
        scheduler = BatchScheduler(
            object(), make_prompt_cache=lambda model: [object()],
            batch_generator_factory=FakeGenerator)
        self.assertFalse(scheduler.probe())
        self.assertIn("cache_without_merge", scheduler.capable_reason or "")
        with self.assertRaises(BatchUnavailable):
            scheduler.start()

    def test_capability_probe_reports_missing_runtime(self):
        scheduler = BatchScheduler(object())
        self.assertFalse(scheduler.probe())
        self.assertEqual(scheduler.capable_reason, "runtime_without_batch_generator")

    def test_shutdown_releases_the_generator(self):
        scheduler = self.make()
        request = BatchRequest("r1", [1], 8)
        scheduler.submit(request)
        drain(request)
        generator = FakeGenerator.instances[0]
        scheduler.shutdown()
        self.assertTrue(generator.closed)


class _BatchCapableAdapter:
    """The subset of the mlx-lm adapter the worker's stream selector uses."""

    def __init__(self, capable=True, decline=None):
        self.capable = capable
        self.decline = decline
        self.single_calls = 0
        self.batch_calls = 0

    def batch_status(self):
        return {"capable": self.capable, "reason": None if self.capable else "disabled"}

    def stream(self, request_id, params):
        self.single_calls += 1
        yield {"type": "delta", "text": "single"}
        yield {"type": "completed", "finish_reason": "stop"}

    def stream_batched(self, request_id, params):
        self.batch_calls += 1
        if self.decline:
            yield {"type": "batch_fallback", "reason": self.decline}
            return
        yield {"type": "delta", "text": "batched"}
        yield {"type": "completed", "finish_reason": "stop"}


class WorkerSelectionTests(unittest.TestCase):
    def test_incapable_adapter_uses_the_single_path(self):
        from common.server import select_stream
        adapter = _BatchCapableAdapter(capable=False)
        events = list(select_stream(adapter, "r1", {}))
        self.assertEqual(events[0]["text"], "single")
        self.assertEqual(adapter.batch_calls, 0)

    def test_capable_adapter_uses_the_batched_path(self):
        from common.server import select_stream
        adapter = _BatchCapableAdapter()
        events = list(select_stream(adapter, "r1", {}))
        self.assertEqual(events[0]["text"], "batched")
        self.assertEqual(adapter.single_calls, 0)

    def test_declined_request_restarts_on_the_single_path(self):
        from common.server import select_stream
        adapter = _BatchCapableAdapter(decline="seed")
        events = list(select_stream(adapter, "r1", {"seed": 1}))
        self.assertEqual([event["type"] for event in events], ["delta", "completed"])
        self.assertEqual(events[0]["text"], "single")
        self.assertEqual(adapter.single_calls, 1)

    def test_adapter_without_batch_api_is_untouched(self):
        from common.server import select_stream

        class Plain:
            def stream(self, request_id, params):
                yield {"type": "delta", "text": "plain"}

        events = list(select_stream(Plain(), "r1", {}))
        self.assertEqual(events[0]["text"], "plain")


class AdapterEligibilityTests(unittest.TestCase):
    """The mlx-lm adapter's own refusal rules (no MLX needed)."""

    def adapter(self, *, kv_bits=None, prompt_cache=None):
        from mlx_lm_worker.adapter import MLXLMAdapter
        adapter = MLXLMAdapter()
        adapter.batch_scheduler = object()
        adapter._batch_capable = True
        adapter.kv_bits = kv_bits
        adapter.prompt_cache = prompt_cache
        return adapter

    def test_plain_text_request_is_eligible(self):
        eligible, reason = self.adapter()._batch_eligible({})
        self.assertTrue(eligible, reason)

    def test_seed_is_refused(self):
        self.assertEqual(self.adapter()._batch_eligible({"seed": 7}), (False, "seed"))

    def test_kv_quantization_is_refused(self):
        self.assertEqual(self.adapter(kv_bits=8)._batch_eligible({}), (False, "kv_quantization"))

    def test_prompt_cache_is_refused(self):
        self.assertEqual(self.adapter(prompt_cache=object())._batch_eligible({}),
                         (False, "prompt_cache"))

    def test_images_are_refused(self):
        self.assertEqual(self.adapter()._batch_eligible({"images": ["/tmp/a.png"]}),
                         (False, "images"))

    def test_adaptive_memory_is_refused(self):
        self.assertEqual(
            self.adapter()._batch_eligible({"adaptiveMemory": {"enabled": True}}),
            (False, "adaptive_memory"))

    def test_disabled_is_refused(self):
        adapter = self.adapter()
        adapter.batch_scheduler = None
        self.assertFalse(adapter._batch_eligible({})[0])

    def test_environment_is_read_fail_safe(self):
        from mlx_lm_worker.adapter import MLXLMAdapter
        adapter = MLXLMAdapter()
        with patch.dict(os.environ, {"MLXBAR_BATCH_ENABLED": "1",
                                     "MLXBAR_BATCH_MAX_SEQUENCES": "99"}, clear=False):
            settings = adapter._batch_settings()
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["maxSequences"], 8)  # clamped, never trusted blindly
        with patch.dict(os.environ, {"MLXBAR_BATCH_MAX_SEQUENCES": "not-a-number"}, clear=False):
            self.assertEqual(adapter._batch_settings()["maxSequences"], 2)


class _BatchWorker:
    instances: list["_BatchWorker"] = []

    def __init__(self, root, settings, *, instance_key="legacy", memory_limit_bytes=0,
                 batch_capable=False, **kwargs):
        self.instance_key = instance_key
        self.memory_limit_bytes = memory_limit_bytes
        self.loaded = None
        self.active_requests: dict = {}
        self.queued_requests: dict = {}
        self.maintenance_engines: set = set()
        self.process = None
        self.socket_path = None
        self.batch_capable = batch_capable
        self.batch_env_at_load: dict = {}
        self.gen_release: asyncio.Event | None = None
        _BatchWorker.instances.append(self)

    async def load(self, model, engine=None, explicit=None, **kwargs):
        self.batch_env_at_load = {name: os.environ.get(name) for name in
                                  ModelPoolSupervisor.BATCH_ENV_NAMES}
        self.loaded = {**model, "engine": engine or model.get("engine"),
                       "capabilities": {
                           "modelMaxTokens": 4096,
                           "memoryLimits": {"set_memory_limit": self.memory_limit_bytes},
                           "continuousBatching": {"capable": self.batch_capable,
                                                  "reason": None if self.batch_capable else "disabled"}}}
        return self.loaded

    async def unload(self):
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    async def _call(self, method, params, timeout=5):
        return {"memory": {"process_rss_bytes": 600 << 20, "active_bytes": 400 << 20,
                           "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        self.active_requests[request_id] = object()
        try:
            if self.gen_release is not None:
                await self.gen_release.wait()
            yield {"type": "delta", "text": str(prompt)}
            yield {"type": "completed", "finish_reason": "stop"}
        finally:
            self.active_requests.pop(request_id, None)

    async def cancel(self, request_id):
        return {"cancelled": request_id in self.active_requests}

    async def prompt_cache_stats(self):
        return {"enabled": False}

    async def clear_memory_prompt_cache(self):
        return {"enabled": False}

    async def clear_disk_prompt_cache(self):
        return {"enabled": False}

    async def wait_until_idle(self, timeout=30):
        return True

    def begin_maintenance(self, engine):
        self.maintenance_engines.add(engine)

    def end_maintenance(self, engine):
        self.maintenance_engines.discard(engine)

    def effective_max_tokens(self):
        return 4096

    def effective_max_prompt_characters(self):
        return 16384

    def raise_if_queue_full(self):
        return None

    def status(self):
        return {"loadedModel": self.loaded, "loadingModel": None,
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests),
                "workerRunning": self.loaded is not None}


class PoolBatchLaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 4, "maxReplicasPerModel": 1,
            "totalMemoryRatio": 0.9, "minimumSystemReserveGB": 1,
            "defaultPerModelMaxGB": 8, "idleTTLSeconds": 30,
            # Two lanes: one for the first request, one for the batched
            # sequence that shares the worker. With a single lane the second
            # request correctly waits for a pool permit, which is a different
            # behaviour than the one this class tests.
            "generationConcurrency": 2,
        })
        _BatchWorker.instances = []
        self.capable = True
        self.patch = patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                           self._worker_factory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _worker_factory(self, *args, **kwargs):
        return _BatchWorker(*args, batch_capable=self.capable, **kwargs)

    def make_pool(self) -> ModelPoolSupervisor:
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 32 * GIB
        pool._host_capacity = lambda: (24 * GIB, 1)
        self.addAsyncCleanup(pool.shutdown)
        return pool

    def profile(self, model_id, **batching):
        entry = {"modelId": model_id, "keepLoaded": False,
                 "continuousBatching": {"enabled": True, "maxSequences": 2}}
        entry["continuousBatching"].update(batching)
        self.settings.data["models"]["pool"]["profiles"] = [entry]

    @staticmethod
    def model(number=1):
        return {"id": f"model-{number}", "name": f"model-{number}",
                "engine": "mlx-lm", "size_bytes": GIB}

    async def test_second_request_shares_the_worker_without_a_replica(self):
        self.profile("model-1")
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        worker = slot.worker
        worker.gen_release = asyncio.Event()
        first = pool.generate_for_model("model-1", "a", [], {}, "r1")
        second = pool.generate_for_model("model-1", "b", [], {}, "r2")
        t1 = asyncio.create_task(anext(first))
        t2 = asyncio.create_task(anext(second))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(worker.active_requests) < 2:
            await asyncio.sleep(0.01)
        self.assertEqual(sorted(worker.active_requests), ["r1", "r2"])
        self.assertEqual(len(slot.gen_active), 2)
        # One slot, one worker: the second request shared it instead of
        # loading a replica. (The legacy supervisor is constructed first and
        # never started while the pool is enabled, so it is not counted.)
        self.assertEqual(len(pool._replica_slots("model-1")), 1)
        self.assertEqual(len(_BatchWorker.instances), 2)
        worker.gen_release.set()
        await t1
        await t2
        await first.aclose()
        await second.aclose()
        self.assertEqual(slot.gen_active, set())
        self.assertEqual(pool._gen_active_lanes, 0)
        self.assertEqual(pool._gen_slots._value, 2)

    async def test_batching_is_off_by_default(self):
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        self.assertEqual(pool._batch_slot_room(slot), 0)
        self.assertFalse(pool._slot_batch_capable(slot))

    async def test_incapable_worker_keeps_the_serial_lane(self):
        self.capable = False
        self.profile("model-1")
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        self.assertFalse(pool._slot_batch_capable(slot))
        self.assertEqual(pool._batch_slot_room(slot), 0)

    async def test_saturated_generation_concurrency_makes_a_batched_request_wait(self):
        self.profile("model-1")
        self.settings.data["models"]["pool"]["generationConcurrency"] = 1
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        # The one lane is already taken by another model.
        await pool._gen_slots.acquire()
        task = asyncio.create_task(pool._acquire_batched_lane(slot, "r1"))
        await asyncio.sleep(0.2)
        # Still waiting for the permit: the cap is not bypassed. (`gen_active`
        # is not asserted here: the lane is registered the moment the permit is
        # granted, and `asyncio.Semaphore.acquire()` yields once even when it
        # must wait.)
        self.assertFalse(task.done())
        self.assertEqual(pool._gen_active_lanes, 0)
        pool._gen_slots.release()  # the other model finishes
        self.assertTrue(await asyncio.wait_for(task, timeout=2))
        # Admitted only now, and the permit it took is the one just released.
        self.assertEqual(slot.gen_active, {"r1"})
        self.assertEqual(pool._gen_active_lanes, 1)
        self.assertEqual(pool._gen_slots._value, 0)
        # Releasing it returns the permit and the lane count.
        self.assertTrue(pool._release_batched_lane(slot, "r1"))
        self.assertEqual(slot.gen_active, set())
        self.assertEqual(pool._gen_active_lanes, 0)
        self.assertEqual(pool._gen_slots._value, 1)

    async def test_sequence_ceiling_is_honoured(self):
        self.profile("model-1", maxSequences=3)
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        slot.gen_active = {"a", "b", "c"}
        self.assertEqual(pool._batch_slot_room(slot), 0)

    async def test_worker_receives_the_profile_environment(self):
        self.profile("model-1", maxSequences=4, prefillBatchSize=3, prefillStepSize=4096)
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        # The legacy supervisor is constructed first (its worker is never
        # started while the pool is enabled); the pool slot's worker is last.
        env = _BatchWorker.instances[-1].batch_env_at_load
        self.assertEqual(env["MLXBAR_BATCH_ENABLED"], "1")
        self.assertEqual(env["MLXBAR_BATCH_MAX_SEQUENCES"], "4")
        self.assertEqual(env["MLXBAR_BATCH_PREFILL_BATCH_SIZE"], "3")
        self.assertEqual(env["MLXBAR_BATCH_PREFILL_STEP_SIZE"], "4096")
        # Scoped to the load: the coordinator's own environment is restored.
        for name in ModelPoolSupervisor.BATCH_ENV_NAMES:
            self.assertIsNone(os.environ.get(name), name)

    async def test_disabled_model_gets_no_batch_environment(self):
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        env = _BatchWorker.instances[-1].batch_env_at_load
        self.assertTrue(all(value is None for value in env.values()))

    async def test_status_reports_batching_per_model(self):
        self.profile("model-1")
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        status = pool.status()
        entry = status["loadedModels"][0]
        self.assertTrue(entry["continuousBatching"]["enabled"])
        self.assertTrue(entry["continuousBatching"]["capable"])
        self.assertEqual(entry["continuousBatching"]["maxSequences"], 2)
        self.assertEqual(entry["batchActiveSequences"], 0)


class BatchSettingsValidationTests(unittest.TestCase):
    def store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SettingsStore(Path(tmp.name))

    def test_valid_profile_is_accepted(self):
        store = self.store()
        store.update({"models": {"pool": {"profiles": [{
            "modelId": "m1", "keepLoaded": False, "replicas": 1, "maxMemoryGB": 8,
            "continuousBatching": {"enabled": True, "maxSequences": 4,
                                   "prefillBatchSize": 2, "prefillStepSize": 2048,
                                   "fallback": True}}]}}})
        profile = store.data["models"]["pool"]["profiles"][0]
        self.assertEqual(profile["continuousBatching"]["maxSequences"], 4)

    def test_sequence_ceiling_is_validated(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.update({"models": {"pool": {"profiles": [{
                "modelId": "m1", "keepLoaded": False,
                "continuousBatching": {"enabled": True, "maxSequences": 9}}]}}})

    def test_boolean_must_be_boolean(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.update({"models": {"pool": {"profiles": [{
                "modelId": "m1", "keepLoaded": False,
                "continuousBatching": {"enabled": "yes"}}]}}})

    def test_object_must_be_an_object(self):
        store = self.store()
        with self.assertRaises(ValueError):
            store.update({"models": {"pool": {"profiles": [{
                "modelId": "m1", "keepLoaded": False,
                "continuousBatching": True}]}}})

    def test_missing_key_keeps_working(self):
        store = self.store()
        store.update({"models": {"pool": {"profiles": [{
            "modelId": "m1", "keepLoaded": True}]}}})
        self.assertNotIn("continuousBatching", store.data["models"]["pool"]["profiles"][0])


if __name__ == "__main__":
    unittest.main()
