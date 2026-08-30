from __future__ import annotations

import argparse
import unittest
import unittest.mock
from unittest.mock import Mock

from mlxbar.cli import execute, remove_all_data


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class LMStudioTokenTests(unittest.TestCase):
    def test_set_lmstudio_token_without_argument_clears_it(self):
        client = Mock()
        client.request.return_value = response({"token": "", "configured": False})
        args = argparse.Namespace(command="secrets", action="set-lmstudio-token", token="")
        execute(args, client)
        client.request.assert_called_once_with("PUT", "/api/v1/settings/lm-studio-token", {"token": ""})


class NetworkTests(unittest.TestCase):
    def test_set_lan_enabled_is_a_single_atomic_put(self):
        client = Mock()
        client.request.return_value = response({"api": {"host": "0.0.0.0"}, "security": {"allowLan": True}})
        args = argparse.Namespace(command="network", action="set-lan", enabled=True, disabled=False)
        execute(args, client)
        self.assertEqual(client.request.call_count, 1)
        method, path, body = client.request.call_args[0]
        self.assertEqual((method, path), ("PUT", "/api/v1/settings"))
        self.assertEqual(body, {
            "api": {"host": "0.0.0.0", "requireToken": True},
            "security": {"allowLan": True},
        })

    def test_set_lan_disabled_is_a_single_atomic_put(self):
        client = Mock()
        client.request.return_value = response({})
        args = argparse.Namespace(command="network", action="set-lan", enabled=False, disabled=True)
        execute(args, client)
        self.assertEqual(client.request.call_count, 1)
        _, _, body = client.request.call_args[0]
        self.assertEqual(body["api"]["host"], "127.0.0.1")
        self.assertEqual(body["security"]["allowLan"], False)


class RuntimeCancelJobTests(unittest.TestCase):
    def test_cancel_job_resolves_job_id_from_runtime_list_first(self):
        client = Mock()
        client.request.side_effect = [
            response({"mlx-lm": {"activeJob": {"id": "job-123"}}}),
            response({"cancelled": True}),
        ]
        args = argparse.Namespace(command="runtime", action="cancel-job", engine="mlx-lm")
        result = execute(args, client)
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args_list[0][0], ("GET", "/api/v1/runtimes"))
        self.assertEqual(client.request.call_args_list[1][0],
                         ("POST", "/api/v1/runtimes/mlx-lm/jobs/job-123/cancel"))
        self.assertEqual(result, {"cancelled": True})

    def test_cancel_job_with_no_active_job_does_not_call_cancel(self):
        client = Mock()
        client.request.return_value = response({"mlx-lm": {"activeJob": None}})
        args = argparse.Namespace(command="runtime", action="cancel-job", engine="mlx-lm")
        result = execute(args, client)
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(result["cancelled"])


class CLIParityTests(unittest.TestCase):
    """Every GUI mutation must be reachable from mlxbarctl (v1.8.1)."""

    def test_unload_all_forwards_force(self):
        client = Mock()
        client.request.return_value = response({"state": "unloaded"})
        args = argparse.Namespace(command="model", action="unload", model_id=None, force=True)
        execute(args, client)
        client.request.assert_called_once_with("DELETE", "/api/v1/models/loaded?force=true")

    def test_unload_all_without_force_is_unchanged(self):
        client = Mock()
        client.request.return_value = response({})
        args = argparse.Namespace(command="model", action="unload", model_id=None, force=False)
        execute(args, client)
        client.request.assert_called_once_with("DELETE", "/api/v1/models/loaded")

    def test_set_replicas_updates_existing_profile_without_loading(self):
        client = Mock()
        client.request.side_effect = [
            response({"models": {"pool": {"profiles": [{"modelId": "m1", "keepLoaded": True}]}}}),
            response({}),
        ]
        args = argparse.Namespace(command="model", action="set-replicas", model_id="m1", count=3)
        execute(args, client)
        self.assertEqual(client.request.call_count, 2)  # GET + PUT, never a load
        method, path, body = client.request.call_args_list[1][0]
        self.assertEqual((method, path), ("PUT", "/api/v1/settings"))
        self.assertEqual(body["models"]["pool"]["profiles"][0]["replicas"], 3)

    def test_set_replicas_pins_an_unpinned_model(self):
        client = Mock()
        client.request.side_effect = [
            response({"models": {"pool": {"profiles": []}}}),
            response({}),
        ]
        args = argparse.Namespace(command="model", action="set-replicas", model_id="new", count=2)
        execute(args, client)
        _, _, body = client.request.call_args_list[1][0]
        self.assertEqual(body["models"]["pool"]["profiles"],
                         [{"modelId": "new", "keepLoaded": True, "replicas": 2}])

    def test_set_replicas_rejects_out_of_range(self):
        client = Mock()
        args = argparse.Namespace(command="model", action="set-replicas", model_id="m1", count=9)
        with self.assertRaises(ValueError):
            execute(args, client)
        client.request.assert_not_called()

    def test_prompt_cache_actions_hit_the_right_endpoints(self):
        for action, expected in (
            ("status", ("GET", "/api/v1/prompt-cache")),
            ("clear-memory", ("POST", "/api/v1/prompt-cache/memory/clear")),
            ("clear-disk", ("POST", "/api/v1/prompt-cache/disk/clear")),
        ):
            client = Mock()
            client.request.return_value = response({})
            args = argparse.Namespace(command="prompt-cache", action=action)
            execute(args, client)
            client.request.assert_called_once_with(*expected)

    def test_prompt_cache_set_builds_a_partial_patch(self):
        client = Mock()
        client.request.return_value = response({})
        args = argparse.Namespace(command="prompt-cache", action="set",
                                  disk_enabled="false", max_gb=None)
        execute(args, client)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings", {"promptCache": {"diskEnabled": False}})

    def test_prompt_cache_set_rejects_bad_size_and_empty(self):
        client = Mock()
        with self.assertRaises(ValueError):
            execute(argparse.Namespace(command="prompt-cache", action="set",
                                       disk_enabled=None, max_gb=200), client)
        with self.assertRaises(ValueError):
            execute(argparse.Namespace(command="prompt-cache", action="set",
                                       disk_enabled=None, max_gb=None), client)
        client.request.assert_not_called()

    def test_set_model_pool_only_sends_provided_options(self):
        client = Mock()
        client.request.return_value = response({})
        args = argparse.Namespace(command="config", action="set-model-pool",
                                  enabled="true", max_resident=3, idle_ttl_seconds=None,
                                  per_model_gb=None, total_memory_percent=80,
                                  system_reserve_gb=None, generation_concurrency=None,
                                  max_replicas_per_model=4)
        execute(args, client)
        _, path, body = client.request.call_args[0]
        self.assertEqual(path, "/api/v1/settings")
        self.assertEqual(body, {"models": {"pool": {
            "enabled": True, "maxResidentModels": 3,
            "totalMemoryRatio": 0.8, "maxReplicasPerModel": 4}}})

    def test_set_model_pool_rejects_out_of_range_and_empty(self):
        client = Mock()
        base = dict(command="config", action="set-model-pool", enabled=None, max_resident=None,
                    idle_ttl_seconds=None, per_model_gb=None, total_memory_percent=None,
                    system_reserve_gb=None, generation_concurrency=None, max_replicas_per_model=None)
        with self.assertRaises(ValueError):
            execute(argparse.Namespace(**{**base, "generation_concurrency": 99}), client)
        with self.assertRaises(ValueError):
            execute(argparse.Namespace(**base), client)
        client.request.assert_not_called()

    def test_set_flag_maps_names_to_dotted_keys(self):
        cases = {
            "auto-load-on-api": {"models": {"autoLoadOnAPIRequest": True}},
            "anthropic-api": {"api": {"anthropic": {"enabled": False}}},
            "remote-image-urls": {"security": {"allowRemoteImageUrls": True}},
            "require-token": {"api": {"requireToken": False}},
            "continue-after-gui-exit": {"general": {"continueAfterGUIExit": True}},
        }
        for name, expected in cases.items():
            value = "true" if True in _flatten(expected) else "false"
            client = Mock()
            client.request.return_value = response({})
            execute(argparse.Namespace(command="config", action="set-flag",
                                       name=name, value=value), client)
            client.request.assert_called_once_with("PUT", "/api/v1/settings", expected)

    def test_lmstudio_base_url_and_auto_load(self):
        client = Mock()
        client.request.return_value = response({})
        execute(argparse.Namespace(command="lmstudio", action="set-base-url",
                                   url="http://127.0.0.1:1234"), client)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings", {"models": {"lmStudio": {"baseUrl": "http://127.0.0.1:1234"}}})
        client = Mock()
        client.request.return_value = response({})
        execute(argparse.Namespace(command="lmstudio", action="set-auto-load", value="false"), client)
        client.request.assert_called_once_with(
            "PUT", "/api/v1/settings", {"models": {"lmStudio": {"autoLoad": False}}})


def _flatten(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten(item)
    else:
        yield value


class RemoveAllDataTests(unittest.TestCase):
    def test_without_yes_never_calls_client(self):
        client = Mock()
        args = argparse.Namespace(command="remove-all-data", yes=False)
        with self.assertRaises(RuntimeError):
            remove_all_data(args, client)
        client.request.assert_not_called()

    def test_with_yes_calls_reset_endpoint(self):
        client = Mock()
        client.socket.exists.return_value = False
        args = argparse.Namespace(command="remove-all-data", yes=True)
        with unittest.mock.patch("mlxbar.cli.subprocess.run") as run:
            result = remove_all_data(args, client)
        client.request.assert_called_once_with("POST", "/api/v1/system/reset")
        self.assertTrue(result["removed"])
        # bootout x2 + defaults delete
        self.assertEqual(run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
