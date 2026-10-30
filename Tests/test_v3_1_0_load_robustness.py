"""v3.1.0 の欠落テスト（WP-911）: mlx-lm のロード耐性（remote code 再試行）.

`trust_remote_code` の 2 段再試行は v3.1.0 で入ったが、テストが無かった。
ここでは `mlx_lm.load` を差し替え、再試行の**順序と条件**だけを固定する
（実際のモデルロードは行わない）:

* TypeError（古い mlx-lm が `tokenizer_config` を知らない）では
  キーワード無しの `load(path)` に落ちる;
* "remote code" を含む失敗だけが `trust_remote_code=True` で再試行される;
* それ以外の失敗は再試行せずそのまま伝播する（無駄な二重ロードをしない）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mlx_lm_worker.adapter import MLXLMAdapter


def make_text_model(root: Path) -> Path:
    path = root / "text-model"
    path.mkdir()
    (path / "config.json").write_text(json.dumps({
        "model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"],
    }), encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 8)
    (path / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"language_model.layers.0.weight": "model.safetensors"},
    }), encoding="utf-8")
    return path


class RemoteCodeRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = make_text_model(self.root)
        self.calls: list[dict] = []

    def tearDown(self):
        sys.modules.pop("mlx_lm", None)
        self.temporary.cleanup()

    def fake_module(self, behaviour):
        """Install a fake `mlx_lm` whose ``load`` records and delegates."""
        module = types.ModuleType("mlx_lm")

        def load(path, **kwargs):
            self.calls.append({"path": path, **kwargs})
            return behaviour(len(self.calls))

        module.load = load
        return module

    def run_load(self, behaviour, path: Path | None = None) -> None:
        with patch.dict(sys.modules, {"mlx_lm": self.fake_module(behaviour)}):
            MLXLMAdapter().load(str(path or self.path))

    def test_remote_code_failure_is_retried_with_the_flag(self):
        def behaviour(call_number):
            if call_number == 1:
                raise RuntimeError("this model needs remote code, please enable it")
            raise RuntimeError("still broken")

        with self.assertRaises(RuntimeError) as raised:
            self.run_load(behaviour)
        self.assertEqual(str(raised.exception), "still broken")
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.calls[0]["tokenizer_config"]["trust_remote_code"])
        self.assertTrue(self.calls[1]["tokenizer_config"]["trust_remote_code"])

    def test_type_error_falls_back_to_a_keywordless_load(self):
        def behaviour(call_number):
            if call_number == 1:
                raise TypeError("load() got an unexpected keyword argument")
            raise RuntimeError("no runtime")

        with self.assertRaises(RuntimeError):
            self.run_load(behaviour)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("tokenizer_config", self.calls[0])
        self.assertNotIn("tokenizer_config", self.calls[1])

    def test_unrelated_failure_is_not_retried(self):
        def behaviour(call_number):
            raise RuntimeError("model architecture not supported")

        with self.assertRaises(RuntimeError) as raised:
            self.run_load(behaviour)
        self.assertIn("architecture", str(raised.exception))
        self.assertEqual(len(self.calls), 1)

    def test_the_vlm_guard_still_refuses_a_vision_directory(self):
        vision = self.root / "vision-model"
        vision.mkdir()
        (vision / "config.json").write_text(json.dumps({
            "model_type": "qwen2_vl",
            "architectures": ["Qwen2VLForConditionalGeneration"],
            "vision_config": {"depth": 1}, "image_token_id": 1,
        }), encoding="utf-8")
        (vision / "model.safetensors").write_bytes(b"\x00" * 16)
        (vision / "tokenizer.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError) as raised:
            self.run_load(lambda call_number: {"ok": True}, vision)
        self.assertIn("MLXBAR_VLM_GUARD", str(raised.exception))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
