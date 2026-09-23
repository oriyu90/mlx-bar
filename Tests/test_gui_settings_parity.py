"""GUI settings panels <-> CLI <-> server contract tests (v2.4.0).

Every settings panel the GUI sends must be accepted by
`SettingsStore.update` with the exact payload shape the Swift code builds,
and every named CLI command must mirror the GUI's ranges. These tests pin
that three-way parity so a GUI/CLI addition cannot drift from validation.
"""
from __future__ import annotations

import argparse
import unittest
import unittest.mock
from unittest.mock import Mock

from mlxbar.cli import execute
from mlxbar.settings import SettingsStore


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def fresh_store(tmp_path):
    return SettingsStore(root=tmp_path)


class ModelPoolHeadroomCLITests(unittest.TestCase):
    def run_pool(self, **kwargs):
        client = Mock()
        client.request.return_value = response({})
        fields = dict(
            command="config", action="set-model-pool",
            enabled=None, max_resident=None, idle_ttl_seconds=None,
            per_model_gb=None, total_memory_percent=None,
            system_reserve_gb=None, generation_concurrency=None,
            max_replicas_per_model=None, per_generation_headroom_gb=None)
        fields.update(kwargs)
        args = argparse.Namespace(**fields)
        execute(args, client)
        return client

    def test_headroom_zero_means_automatic(self):
        client = self.run_pool(per_generation_headroom_gb=0.0)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"models": {"pool": {"perGenerationHeadroomGB": 0.0}}})

    def test_headroom_mid_range_accepted(self):
        client = self.run_pool(per_generation_headroom_gb=2.5)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"models": {"pool": {"perGenerationHeadroomGB": 2.5}}})

    def test_headroom_small_positive_rejected(self):
        with self.assertRaises(ValueError):
            self.run_pool(per_generation_headroom_gb=0.1)

    def test_headroom_above_ceiling_rejected(self):
        with self.assertRaises(ValueError):
            self.run_pool(per_generation_headroom_gb=32.5)


class ModelPoolGUIContractTests(unittest.TestCase):
    """Payloads shaped exactly like MenuBarViewModel.setModelPoolSettings."""

    def test_pool_payload_with_max_replicas_accepted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(Path(directory))
            public = store.update({"models": {"pool": {
                "enabled": True, "maxResidentModels": 2,
                "idleTTLSeconds": 900, "defaultPerModelMaxGB": 32,
                "totalMemoryRatio": 0.75, "minimumSystemReserveGB": 4,
                "generationConcurrency": 2, "maxReplicasPerModel": 3,
            }}})
            self.assertEqual(public["models"]["pool"]["maxReplicasPerModel"], 3)

    def test_headroom_payload_accepted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(Path(directory))
            public = store.update({"models": {"pool": {"perGenerationHeadroomGB": 0}}})
            self.assertEqual(public["models"]["pool"]["perGenerationHeadroomGB"], 0)
            public = store.update({"models": {"pool": {"perGenerationHeadroomGB": 0.25}}})
            self.assertEqual(public["models"]["pool"]["perGenerationHeadroomGB"], 0.25)

    def test_headroom_payload_rejected(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(Path(directory))
            with self.assertRaises(ValueError):
                store.update({"models": {"pool": {"perGenerationHeadroomGB": 0.1}}})

    def test_per_model_memory_payload_preserves_replicas(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(Path(directory))
            store.update({"models": {"pool": {"profiles": [
                {"modelId": "m1", "keepLoaded": True, "replicas": 2}]}}})
            # Mirror MenuBarViewModel.setModelMemoryLimit: rewrite the array
            # with only maxMemoryGB changed on the matching entry.
            pool = store.public()["models"]["pool"]
            profiles = [dict(item) for item in pool["profiles"]]
            for profile in profiles:
                if profile.get("modelId") == "m1":
                    profile["maxMemoryGB"] = 48
            public = store.update({"models": {"pool": {"profiles": profiles}}})
            entry = public["models"]["pool"]["profiles"][0]
            self.assertEqual(entry["maxMemoryGB"], 48)
            self.assertEqual(entry["replicas"], 2)
            self.assertTrue(entry["keepLoaded"])


class PromptCacheAdvancedTests(unittest.TestCase):
    def run_set(self, **kwargs):
        client = Mock()
        client.request.return_value = response({})
        fields = dict(command="prompt-cache", action="set",
                      disk_enabled=None, max_gb=None,
                      keep_generations=None, memory_ratio=None,
                      branch_checkpoint=None, write_budget_gb=None)
        fields.update(kwargs)
        execute(argparse.Namespace(**fields), client)
        return client

    def test_advanced_options_sent_together(self):
        client = self.run_set(keep_generations=4, memory_ratio=0.2,
                              branch_checkpoint="off", write_budget_gb=16.0)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"promptCache": {"keepGenerations": 4, "memoryRatio": 0.2,
                             "branchCheckpoint": "off", "diskWriteBudgetGB": 16.0}})

    def test_advanced_ranges_rejected(self):
        for kwargs in ({"keep_generations": 0}, {"keep_generations": 11},
                       {"memory_ratio": -0.1}, {"memory_ratio": 0.6},
                       {"write_budget_gb": -1}, {"write_budget_gb": 4097}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                self.run_set(**kwargs)

    def test_gui_payload_accepted_by_server(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            # Mirror MenuBarViewModel.setPromptCacheAdvanced.
            public = store.update({"promptCache": {
                "keepGenerations": 4, "memoryRatio": 0.2,
                "branchCheckpoint": "off", "diskWriteBudgetGB": 16.0}})
            cache = public["promptCache"]
            self.assertEqual(
                (cache["keepGenerations"], cache["memoryRatio"],
                 cache["branchCheckpoint"], cache["diskWriteBudgetGB"]),
                (4, 0.2, "off", 16.0))


class GenerationLimitsTests(unittest.TestCase):
    def run_gen(self, **kwargs):
        client = Mock()
        client.request.return_value = response({})
        fields = dict(
            command="config", action="set-generation-limits",
            max_prompt_characters=None, max_images=None, max_image_bytes=None,
            load_timeout_seconds=None, token_idle_timeout_seconds=None,
            stream_heartbeat_seconds=None, total_timeout_seconds=None,
            cancel_grace_seconds=None, memory_limit_ratio=None,
            wired_limit_ratio=None, cache_limit_ratio=None)
        fields.update(kwargs)
        execute(argparse.Namespace(**fields), client)
        return client

    def test_full_payload_sent(self):
        client = self.run_gen(
            max_prompt_characters=50000, max_images=4, max_image_bytes=10485760,
            load_timeout_seconds=300, token_idle_timeout_seconds=30,
            stream_heartbeat_seconds=5, total_timeout_seconds=1800,
            cancel_grace_seconds=3, memory_limit_ratio=0.85,
            wired_limit_ratio=0.7, cache_limit_ratio=0.05)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"generation": {
                "maxPromptCharacters": 50000, "maxImages": 4,
                "maxImageBytes": 10485760, "loadTimeoutSeconds": 300,
                "tokenIdleTimeoutSeconds": 30, "streamHeartbeatSeconds": 5,
                "totalTimeoutSeconds": 1800, "cancelGraceSeconds": 3,
                "memoryLimitRatio": 0.85, "wiredLimitRatio": 0.7,
                "cacheLimitRatio": 0.05}})

    def test_ranges_rejected(self):
        for kwargs in ({"max_prompt_characters": 0},
                       {"max_images": 129}, {"max_images": -1},
                       {"load_timeout_seconds": 9},
                       {"token_idle_timeout_seconds": 601},
                       {"stream_heartbeat_seconds": 31},
                       {"total_timeout_seconds": 7201},
                       {"cancel_grace_seconds": 0},
                       {"memory_limit_ratio": 0.49},
                       {"memory_limit_ratio": 1.0},
                       {"wired_limit_ratio": 0.96},
                       {"cache_limit_ratio": 0.51}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                self.run_gen(**kwargs)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gen()

    def test_gui_payload_accepted_by_server(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            # Mirror the three Swift setters.
            public = store.update({"generation": {
                "maxPromptCharacters": 50000, "maxImages": 4,
                "maxImageBytes": 10485760}})
            self.assertEqual(public["generation"]["maxImages"], 4)
            public = store.update({"generation": {
                "loadTimeoutSeconds": 300, "tokenIdleTimeoutSeconds": 30,
                "streamHeartbeatSeconds": 5, "totalTimeoutSeconds": 1800,
                "cancelGraceSeconds": 3}})
            self.assertEqual(public["generation"]["cancelGraceSeconds"], 3)
            public = store.update({"generation": {
                "memoryLimitRatio": 0.85, "wiredLimitRatio": 0.7,
                "cacheLimitRatio": 0.05}})
            self.assertEqual(public["generation"]["wiredLimitRatio"], 0.7)
            # Cross-field rule still enforced server-side.
            with self.assertRaises(ValueError):
                store.update({"generation": {"wiredLimitRatio": 0.9}})


class ApiLimitsTests(unittest.TestCase):
    def run_api(self, **kwargs):
        client = Mock()
        client.request.return_value = response({})
        fields = dict(command="config", action="set-api-limits",
                      max_request_bytes=None, max_concurrent_connections=None)
        fields.update(kwargs)
        execute(argparse.Namespace(**fields), client)
        return client

    def test_limits_sent(self):
        client = self.run_api(max_request_bytes=0,
                              max_concurrent_connections=128)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"api": {"maxRequestBytes": 0, "maxConcurrentConnections": 128}})

    def test_ranges_rejected(self):
        with self.assertRaises(ValueError):
            self.run_api(max_request_bytes=-1)
        with self.assertRaises(ValueError):
            self.run_api(max_concurrent_connections=0)
        with self.assertRaises(ValueError):
            self.run_api(max_concurrent_connections=1025)
        with self.assertRaises(ValueError):
            self.run_api()

    def test_gui_payload_accepted_by_server(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            # Mirror MenuBarViewModel.setApiLimits.
            public = store.update({"api": {"maxRequestBytes": 0,
                                           "maxConcurrentConnections": 128}})
            self.assertEqual(public["api"]["maxConcurrentConnections"], 128)


class MiscFlagTests(unittest.TestCase):
    def test_log_level_and_new_flags(self):
        client = Mock()
        client.request.return_value = response({})
        execute(argparse.Namespace(command="config", action="set-log-level",
                                   level="debug"), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings", {"general": {"logLevel": "debug"}})
        execute(argparse.Namespace(command="config", action="set-flag",
                                   name="preload-last-model", value="true"), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings", {"general": {"preloadLastModel": True}})
        execute(argparse.Namespace(command="config", action="set-flag",
                                   name="lmstudio-enabled", value="false"), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings",
            {"models": {"lmStudio": {"enabled": False}}})

    def test_lmstudio_enabled_and_folder(self):
        client = Mock()
        client.request.return_value = response({})
        execute(argparse.Namespace(command="lmstudio", action="set-enabled",
                                   value="false"), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings",
            {"models": {"lmStudio": {"enabled": False}}})
        execute(argparse.Namespace(command="lmstudio", action="set-folder",
                                   path="/tmp/models"), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings",
            {"models": {"lmStudio": {"folder": "/tmp/models"}}})
        # Empty path clears back to None (the default).
        execute(argparse.Namespace(command="lmstudio", action="set-folder",
                                   path=""), client)
        client.request.assert_called_with(
            "PUT", "/api/v1/settings",
            {"models": {"lmStudio": {"folder": None}}})

    def test_gui_payloads_accepted_by_server(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            public = store.update({"general": {"logLevel": "debug"}})
            self.assertEqual(public["general"]["logLevel"], "debug")
            public = store.update({"models": {"lmStudio": {"enabled": False}}})
            self.assertFalse(public["models"]["lmStudio"]["enabled"])
            public = store.update({"models": {"lmStudio": {"folder": None}}})
            self.assertIsNone(public["models"]["lmStudio"]["folder"])


class RagAdvancedTests(unittest.TestCase):
    def test_extended_gui_payload_accepted_by_server(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            # Mirror MenuBarViewModel.setRagSettings with the new arguments.
            public = store.update({"rag": {
                "enabled": True,
                "embedding": {"baseUrl": "http://127.0.0.1:1234/v1",
                              "model": "m", "timeoutSeconds": 60,
                              "batchSize": 64},
                "chunkSize": 1000, "chunkOverlap": 200,
                "defaultTopK": 4, "maxContextChars": 6000,
                "maxChunksPerCollection": 8000}})
            rag = public["rag"]
            self.assertEqual(rag["embedding"]["timeoutSeconds"], 60)
            self.assertEqual(rag["embedding"]["batchSize"], 64)
            self.assertEqual(rag["maxChunksPerCollection"], 8000)
            with self.assertRaises(ValueError):
                store.update({"rag": {"embedding": {"timeoutSeconds": 4}}})
            with self.assertRaises(ValueError):
                store.update({"rag": {"maxChunksPerCollection": 99}})


class AdaptiveFallbackTests(unittest.TestCase):
    def test_fallback_flag_sent(self):
        client = Mock()
        client.request.return_value = response({})
        fields = dict(
            command="config", action="set-adaptive-memory",
            enabled=None, policy=None, trigger_percent=None,
            memory_pressure_percent=None, keep_tail=None,
            max_latent_tokens=None, max_retrieved_segments=None,
            verbatim_protection=None, soft_token=None,
            fallback_to_exact="false")
        execute(argparse.Namespace(**fields), client)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings",
            {"experimental": {"adaptiveMemory": {"fallbackToExact": False}}})

    def test_future_gates_stay_off(self):
        # hybridKVReuse / memoryTier have no GUI or CLI mutation path; the
        # server must keep rejecting attempts to enable them.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            with self.assertRaises(ValueError):
                store.update({"experimental": {"adaptiveMemory":
                                               {"hybridKVReuse": True}}})
            with self.assertRaises(ValueError):
                store.update({"experimental": {"pagedKVCache":
                                               {"memoryTier": "auto"}}})
            public = store.update({"experimental": {"adaptiveMemory":
                                                    {"fallbackToExact": False}}})
            self.assertFalse(public["experimental"]["adaptiveMemory"]
                             ["fallbackToExact"])


class LogLevelTests(unittest.TestCase):
    def test_valid_levels_accepted(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            for level in ("debug", "info", "warning", "error"):
                public = store.update({"general": {"logLevel": level}})
                self.assertEqual(public["general"]["logLevel"], level)

    def test_invalid_level_rejected(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(root=Path(directory))
            for level in ("verbose", "INFO", "", None):
                with self.assertRaises(ValueError, msg=str(level)):
                    store.update({"general": {"logLevel": level}})

    def test_server_log_level_wiring(self):
        from mlxbar.main import server_log_level, SERVER_LOG_LEVELS
        from mlxbar.settings import DEFAULTS
        from copy import deepcopy

        class FakeSettings:
            def __init__(self, data):
                self.data = data

        self.assertEqual(tuple(SERVER_LOG_LEVELS),
                         ("debug", "info", "warning", "error"))
        for level in SERVER_LOG_LEVELS:
            data = deepcopy(DEFAULTS)
            data["general"]["logLevel"] = level
            self.assertEqual(server_log_level(FakeSettings(data)), level)
        # Anything unexpected (e.g. a hand-edited config) falls back to
        # warning instead of crashing uvicorn at listener startup.
        data = deepcopy(DEFAULTS)
        data["general"]["logLevel"] = "verbose"
        self.assertEqual(server_log_level(FakeSettings(data)), "warning")
        self.assertEqual(server_log_level(FakeSettings({})), "warning")


if __name__ == "__main__":
    unittest.main()
