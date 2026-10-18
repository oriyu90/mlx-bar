from __future__ import annotations

import argparse
import asyncio
import ast
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from mlxbar.cli import execute
from mlxbar.errors import MLXBarError
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class FakeWorker:
    instances = []
    load_calls = 0
    observed_bytes = 768 << 20

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
        self.unloaded = False
        self.gen_started = None
        self.gen_release = None
        FakeWorker.instances.append(self)

    @staticmethod
    def _is_our_worker(pid): return False

    async def load(self, model, engine=None):
        FakeWorker.load_calls += 1
        self.loaded = {**model, "engine": engine or model.get("engine"),
                       "capabilities": {"modelMaxTokens": 4096, "memoryLimits": {
                           "set_memory_limit": self.memory_limit_bytes}}}
        self.engine = self.loaded["engine"]
        return self.loaded

    async def unload(self):
        self.unloaded = True
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    async def _call(self, method, params, timeout=5):
        return {"memory": {"process_rss_bytes": FakeWorker.observed_bytes,
                           "active_bytes": 512 << 20,
                           "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        self.active_requests[request_id] = object()
        try:
            await asyncio.sleep(0)
            yield {"type": "delta", "text": str(prompt)}
        finally:
            self.active_requests.pop(request_id, None)

    async def cancel(self, request_id): return {"cancelled": request_id in self.active_requests}
    async def prompt_cache_stats(self): return {"enabled": False}
    async def clear_memory_prompt_cache(self): return {"enabled": False}
    async def clear_disk_prompt_cache(self): return {"enabled": False}
    async def wait_until_idle(self, timeout=30): return True
    async def probe_runtime(self, engine): return {"ok": True, "engine": engine}
    def begin_maintenance(self, engine): self.maintenance_engines.add(engine)
    def end_maintenance(self, engine): self.maintenance_engines.discard(engine)
    def effective_max_tokens(self): return 4096
    def effective_max_prompt_characters(self): return 16384
    def raise_if_queue_full(self): return None
    def status(self):
        return {"loadedModel": self.loaded, "loadingModel": self.loading,
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests)}


def make_pool(settings=None):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    store = settings or SettingsStore(root)
    store.data["models"]["pool"].update({
        "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
        "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 8,
        "idleTTLSeconds": 30,
    })
    pool = ModelPoolSupervisor(root, store)
    pool._physical_memory = lambda: 32 * GIB
    pool._host_capacity = lambda: (24 * GIB, 1)
    return temporary, pool


def model(number, size_gb=1):
    return {"id": f"model-{number}", "name": f"model-{number}",
            "engine": "mlx-lm", "size_bytes": int(size_gb * GIB)}


class ReloadResidentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeWorker.instances = []
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self.temporary, self.pool = make_pool()
        self.pool.settings.data["models"]["pool"]["maxResidentModels"] = 1

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def test_transient_listed_first_never_displaces_pinned(self):
        # Snapshots arrive in storage (id-sorted) order with the transient
        # model first; the pinned model must win the single resident slot.
        # v2.8.0: the transient is no longer refused -- it comes back as a
        # non-resident, which does not displace the pinned resident.
        snapshots = [
            {"model": model(2), "engine": "mlx-lm", "sessionPinned": False},
            {"model": model(1), "engine": "mlx-lm", "sessionPinned": True},
        ]
        results = await self.pool.reload_resident(snapshots, "mlx-lm")
        self.assertEqual(len(results), 2)
        self.assertIn("model-1", self.pool._slots)
        self.assertIn("model-2", self.pool._slots)
        self.assertTrue(self.pool._slots["model-1"].session_pinned)
        self.assertTrue(self.pool._slots["model-1"].resident)
        self.assertFalse(self.pool._slots["model-2"].resident)

    async def test_transient_reload_is_transient(self):
        snapshots = [{"model": model(2), "engine": "mlx-lm", "sessionPinned": False}]
        await self.pool.reload_resident(snapshots, "mlx-lm")
        self.assertFalse(self.pool._slots["model-2"].session_pinned)

    async def test_pinned_failure_still_raises(self):
        self.pool.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 1
        snapshots = [
            {"model": model(1, size_gb=1), "engine": "mlx-lm", "sessionPinned": True},
            {"model": model(2), "engine": "mlx-lm", "sessionPinned": False},
        ]
        with self.assertRaises(MLXBarError):
            await self.pool.reload_resident(snapshots, "mlx-lm")


class PreloadPinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeWorker.instances = []
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self.temporary, self.pool = make_pool()
        self.pool.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "model-1", "keepLoaded": True},
        ]

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    def make_state(self, last_id):
        from mlxbar import main as main_module

        catalog = {f"model-{number}": model(number) for number in (1, 2)}
        database = Mock()
        database.metadata_value.side_effect = lambda key: {
            "api_autoload_suspended": "0",
            "last_loaded_model_id": last_id,
        }.get(key)
        database.list_models.return_value = list(catalog.values())
        state = Mock()
        state.database = database
        state.settings = self.pool.settings
        state.workers = self.pool
        state.model_autoload_lock = asyncio.Lock()
        return state, main_module

    async def test_last_model_preloads_transient(self):
        state, main_module = self.make_state("model-2")
        task = main_module._preload_last_model(state)
        self.assertIsNotNone(task)
        await task
        self.assertTrue(self.pool._slots["model-1"].session_pinned)
        self.assertIn("model-2", self.pool._slots)
        self.assertFalse(self.pool._slots["model-2"].session_pinned)

    async def test_last_model_equal_to_pinned_loads_once_pinned(self):
        state, main_module = self.make_state("model-1")
        await main_module._preload_last_model(state)
        self.assertEqual(set(self.pool._slots), {"model-1"})
        self.assertTrue(self.pool._slots["model-1"].session_pinned)


class UnpinReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeWorker.instances = []
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self.temporary, self.pool = make_pool()
        self.pool.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "model-1", "keepLoaded": True},
        ]

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def test_unpin_clears_session_pin_but_keeps_manual_pin(self):
        from mlxbar.api.management import _release_unpinned_sessions

        await self.pool.load(model(1), pin=True)
        await self.pool.load(model(2), pin=True)
        app = Mock()
        app.settings = self.pool.settings
        app.workers = self.pool
        old = {"model-1": True}
        # model-1 unpinned via profiles; model-2 was never in profiles.
        self.pool.settings.data["models"]["pool"]["profiles"] = []
        _release_unpinned_sessions(app, old)
        self.assertFalse(self.pool._slots["model-1"].session_pinned)
        self.assertTrue(self.pool._slots["model-2"].session_pinned)

    async def test_unpinned_model_becomes_ttl_eligible(self):
        from mlxbar.api.management import _release_unpinned_sessions

        await self.pool.load(model(1), pin=True)
        app = Mock()
        app.settings = self.pool.settings
        app.workers = self.pool
        self.pool.settings.data["models"]["pool"]["profiles"] = []
        _release_unpinned_sessions(app, {"model-1": True})
        for slot in self.pool._slots.values():
            slot.last_released_at = time.monotonic() - 31
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertFalse(self.pool._slots)


class LoadPinParamTests(unittest.TestCase):
    def test_model_load_defaults_to_pin(self):
        client = Mock()
        client.request.return_value = response({"id": "model-1"})
        args = argparse.Namespace(command="model", action="load",
                                  model_id="model-1", engine="auto", no_pin=False)
        execute(args, client)
        _, _, body = client.request.call_args[0]
        self.assertEqual(body, {"engine": "auto", "pin": True})

    def test_model_load_no_pin_sends_false(self):
        client = Mock()
        client.request.return_value = response({"id": "model-1"})
        args = argparse.Namespace(command="model", action="load",
                                  model_id="model-1", engine="auto", no_pin=True)
        execute(args, client)
        _, _, body = client.request.call_args[0]
        self.assertEqual(body, {"engine": "auto", "pin": False})


class ProbeContractTests(unittest.TestCase):
    @staticmethod
    def probe_code():
        path = Path(__file__).resolve().parent.parent / "Coordinator" / "mlxbar" / "runtimes" / "updater.py"
        if not path.exists():
            path = Path("Coordinator/mlxbar/runtimes/updater.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "code":
                return ast.literal_eval(node.value)
        raise AssertionError("probe snippet not found")

    def test_probe_requires_request_time_template_and_reports_warnings(self):
        code = self.probe_code()
        # Hard: every vlm text generation renders through apply_chat_template.
        self.assertIn("from mlx_vlm.prompt_utils import apply_chat_template", code)
        self.assertIn("assert callable(apply_chat_template)", code)
        # Soft: recorded into probe.json instead of failing the stage.
        self.assertIn("'warnings':warnings", code.replace(" ", ""))
        for marker in ("mlx_vlm.models.cache', 'make_prompt_cache",
                       "mlx_vlm.generate', 'PromptCacheState",
                       "mlx_vlm.apc', 'APCManager",
                       "mlx_vlm.tools', '_infer_tool_parser_from_processor",
                       "mlx_lm.models.cache', 'make_prompt_cache",
                       "mlx_lm.sample_utils', 'make_sampler"):
            self.assertIn(marker.replace(" ", ""), code.replace(" ", ""))

    def test_probe_parses_as_valid_python(self):
        compile(self.probe_code(), "<probe>", "exec")


class VlmFinalizeTests(unittest.TestCase):
    @staticmethod
    def load_adapter():
        workers = Path(__file__).resolve().parent.parent / "Workers"
        if not workers.exists():
            workers = Path("Workers")
        sys.path.insert(0, str(workers))
        try:
            from mlx_vlm_worker.adapter import MLXVLMAdapter
            return MLXVLMAdapter()
        finally:
            sys.path.remove(str(workers))

    def test_finalize_prefers_new_tools_registry(self):
        adapter = self.load_adapter()
        adapter.processor = object()
        tools_module = ModuleType("mlx_vlm.tools")
        tools_module._infer_tool_parser_from_processor = lambda processor: "dummy"
        tools_module.load_tool_module = lambda parser: object()
        vlm_module = ModuleType("mlx_vlm")
        server_module = ModuleType("mlx_vlm.server")
        responses_module = ModuleType("mlx_vlm.server.responses_state")
        responses_module.process_tool_calls = lambda text, module, tools: {
            "calls": [{"function": {"name": "ping", "arguments": "{}"}}],
            "remaining_text": "done"}
        with patch.dict(sys.modules, {"mlx_vlm": vlm_module,
                                      "mlx_vlm.tools": tools_module,
                                      "mlx_vlm.server": server_module,
                                      "mlx_vlm.server.responses_state": responses_module}):
            result = adapter.finalize("hello", {"tools": [{"name": "ping"}]})
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "ping")
        self.assertEqual(result["text"], "done")

    def test_finalize_falls_back_to_markup_without_registry(self):
        adapter = self.load_adapter()
        adapter.processor = object()
        saved = {key: sys.modules[key] for key in
                 [key for key in sys.modules if key == "mlx_vlm" or key.startswith("mlx_vlm.")] }
        for key in saved:
            del sys.modules[key]
        try:
            result = adapter.finalize(
                '<assistant><tool_call>ping'
                '<arg_key>value</arg_key><arg_value>7</arg_value></tool_call></assistant>',
                {"tools": [{"name": "ping"}]})
        finally:
            sys.modules.update(saved)
        self.assertTrue(result.get("tool_calls"))


if __name__ == "__main__":
    unittest.main()
