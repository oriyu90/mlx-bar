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
