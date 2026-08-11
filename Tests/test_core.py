from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from mlxbar.catalog.classifier import classify
from mlxbar.catalog.scanner import normalized_id
from mlxbar.cli import execute
from mlxbar.settings import SettingsStore
from mlxbar.state import AppState


class ClassifierTests(unittest.TestCase):
    def test_gguf_is_provider_only(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "model.gguf").touch()
            result = classify(Path(directory))
            self.assertEqual(result["format"], "gguf")
            self.assertEqual(result["engine"], "lm-studio")

    def test_vlm_requires_processor_and_vision_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"vision_config": {}}))
            (root / "tokenizer.json").touch()
            (root / "model.safetensors").touch()
            (root / "preprocessor_config.json").touch()
            self.assertEqual(classify(root)["format"], "mlx_vlm")

    def test_text_only_laguna_is_routed_to_mlx_vlm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps({"model_type": "laguna"}))
            (root / "tokenizer.json").touch()
            (root / "model.safetensors").touch()
            result = classify(root)
            self.assertEqual(result["engine"], "mlx-vlm")
            self.assertEqual(result["modalities"], ["text"])

    def test_normalized_id_is_stable_and_scoped(self):
        self.assertEqual(normalized_id("a", "/model"), normalized_id("a", "/model"))
        self.assertNotEqual(normalized_id("a", "/model"), normalized_id("b", "/model"))


class SettingsTests(unittest.TestCase):
    def test_english_is_default_and_only_supported_gui_languages_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            self.assertEqual(store.data["general"]["language"], "en")
            self.assertEqual(store.update({"general": {"language": "ja"}})["general"]["language"], "ja")
            with self.assertRaisesRegex(ValueError, "general.language"):
                store.update({"general": {"language": "fr"}})

    def test_unknown_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            store.update({"future": {"answer": 42}})
            store.update({"api": {"port": 12000}})
            self.assertEqual(store.data["future"]["answer"], 42)

    def test_invalid_port_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            with self.assertRaises(ValueError):
                store.update({"api": {"port": 80}})

    def test_lan_access_requires_matching_host_and_api_token(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            enabled = store.update({
                "api": {"host": "0.0.0.0", "requireToken": True},
                "security": {"allowLan": True},
            })
            self.assertTrue(enabled["security"]["allowLan"])
            self.assertEqual(enabled["api"]["host"], "0.0.0.0")
            with self.assertRaisesRegex(ValueError, "APIキー"):
                store.update({"api": {"requireToken": False}})

    def test_lan_host_and_switch_must_change_together(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            with self.assertRaisesRegex(ValueError, "changed together"):
                store.update({"api": {"host": "0.0.0.0"}})

    def test_token_is_not_in_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            self.assertNotIn(store.api_token, json.dumps(store.public()))
            self.assertEqual(store.token_path.stat().st_mode & 0o777, 0o600)

    def test_api_and_lm_studio_tokens_can_be_changed_without_entering_config(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            custom = "custom-api-key-1234567890"
            self.assertEqual(store.set_api_token(custom), custom)
            self.assertEqual(store.api_token, custom)
            self.assertNotIn(custom, json.dumps(store.public()))
            regenerated = store.regenerate_token()
            self.assertNotEqual(regenerated, custom)
            store.set_lm_studio_token("lm-studio-secret")
            self.assertEqual(store.lm_studio_token, "lm-studio-secret")
            self.assertNotIn("lm-studio-secret", json.dumps(store.public()))
            self.assertEqual(store.lm_studio_token_path.stat().st_mode & 0o777, 0o600)
            store.set_lm_studio_token("")
            self.assertIsNone(store.lm_studio_token)


class PortTests(unittest.TestCase):
    def test_range_validation(self):
        self.assertFalse(AppState.test_port(80)["available"])
        self.assertEqual(AppState.test_port(80)["code"], "INVALID_PORT")

    def test_listener_host_is_restricted(self):
        self.assertEqual(AppState.test_port(12000, "192.0.2.10")["code"], "INVALID_HOST")


class RuntimeBootstrapTests(unittest.TestCase):
    def test_missing_runtimes_are_automatically_scheduled(self):
        state = AppState.__new__(AppState)
        state.settings = type("Settings", (), {"data": {"runtimes": {"autoInstallMissing": True}}})()
        state.slots = type("Slots", (), {"active": lambda _self, engine: {"active": "slot" if engine == "mlx-lm" else None}})()
        state.runtime_update_job = Mock(return_value={"id": "job"})
        jobs = state.install_missing_runtimes()
        self.assertEqual(jobs, [{"id": "job"}])
        state.runtime_update_job.assert_called_once_with("mlx-vlm")


class CLITests(unittest.TestCase):
    def test_generate_stream_error_is_not_reported_as_success(self):
        class Response:
            def iter_lines(self):
                yield 'data: {"type":"error","code":"MODEL_NOT_LOADED","message":"モデル未ロード"}'

        class Client:
            def request(self, *_args, **_kwargs):
                return Response()

        args = type("Args", (), {"command": "generate", "prompt": "test", "image": [],
                                  "temperature": 0.7, "max_tokens": 10, "request_id": "test"})()
        with self.assertRaisesRegex(RuntimeError, "モデル未ロード"):
            execute(args, Client())


if __name__ == "__main__":
    unittest.main()
