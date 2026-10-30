"""v3.2.0 (ISS-2 / WP-210): model capabilities, runtime fit and engine choice.

The classification used to be a single "is this a VLM" boolean derived from
`config.json` keys. That could not express an artifact whose config *declares*
a vision tower while the checkpoint ships no vision weights (Qwen3.8-Flash-Next
Niwaki: 0 of 3434 tensors) nor a `language_model_only` text export. It also
allowed the catalog engine and the worker guard to drift apart.

These tests pin the three properties the fix relies on:

* `capabilities.vision` needs a vision marker **and** real vision tensors;
* the serving `engine` is decided independently of `capabilities` (qwen4_exp is
  served only by mlx-vlm, with or without vision);
* the coordinator and the worker agree on one directory (AC-2-4).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from common import vision as worker_vision
from mlxbar.catalog.classifier import classify, detect_engine
from mlxbar.database import Database

VISION_TENSOR = "vision_tower.merger.linear_fc1.weight"

TEXT_CONFIG = {"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}
VISION_CONFIG = {
    "model_type": "qwen3_5_moe",
    "architectures": ["Qwen3_5MoeForConditionalGeneration"],
    "vision_config": {"depth": 27, "hidden_size": 1152},
    "image_token_id": 151655,
    "video_token_id": 151656,
}
# Niwaki: the config advertises a vision tower, the weights do not have one.
NIWAKI_CONFIG = {
    "model_type": "qwen4_exp",
    "architectures": ["Qwen4ExpForConditionalGeneration"],
    "vision_config": {"depth": 27},
    "image_token_id": 151655,
    "language_model_only": False,
    "niwaki": {"shared_only_layers": [], "kept_experts": {}, "kept_routing": {}},
}
NESTED_CONFIG = {
    "model_type": "qwen3_5_moe",
    "architectures": ["Qwen3_5MoeForConditionalGeneration"],
    "text_config": {"vision_config": {"depth": 27}, "image_token_id": 151655},
}


def make_model(root: Path, name: str, config: dict, *, tensors=(), index=True,
               tokenizer=True) -> Path:
    """A model directory with a real ``config.json`` and (optionally) an index."""
    path = root / name
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if tokenizer:
        (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"\x00" * 8)
    if index:
        weight_map = {f"language_model.layers.{i}.weight": "model.safetensors"
                      for i in range(3)}
        for tensor in tensors:
            weight_map[tensor] = "model.safetensors"
        (path / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": weight_map}), encoding="utf-8")
    return path


class CapabilityClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_text_model_has_no_vision_capability(self):
        path = make_model(self.root, "text", TEXT_CONFIG)
        result = classify(path)
        self.assertFalse(result["capabilities"]["vision"])
        self.assertTrue(result["capabilities"]["text"])
        self.assertEqual(result["engine"], "mlx-lm")

    def test_vision_config_with_vision_tensors_is_image_capable(self):
        path = make_model(self.root, "ornith", VISION_CONFIG, tensors=[VISION_TENSOR])
        result = classify(path)
        self.assertTrue(result["capabilities"]["vision"])
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertIn("model.safetensors.index.json",
                      json.dumps(result["evidence"], ensure_ascii=False))

    def test_vision_config_without_vision_tensors_is_text_only(self):
        """Niwaki's real shape: vision_config declared, 0 vision tensors.

        `vision_config` alone must not advertise image input -- the load would
        build a vision tower with no weights and fail on strict load.
        """
        path = make_model(self.root, "niwaki", NIWAKI_CONFIG)
        result = classify(path)
        self.assertFalse(result["capabilities"]["vision"])
        self.assertEqual(result["evidence"]["tensors"]["vision"], 0)

    def test_language_model_only_wins_over_vision_tensors(self):
        config = dict(VISION_CONFIG, language_model_only=True)
        path = make_model(self.root, "text-export", config, tensors=[VISION_TENSOR])
        self.assertFalse(classify(path)["capabilities"]["vision"])

    def test_nested_text_config_vision_is_detected(self):
        path = make_model(self.root, "nested", NESTED_CONFIG, tensors=[VISION_TENSOR])
        self.assertTrue(classify(path)["capabilities"]["vision"])

    def test_engine_is_independent_of_the_vision_capability(self):
        """qwen4_exp is served only by mlx-vlm, so a text-only capability must
        not demote the engine to mlx-lm (which has no implementation)."""
        path = make_model(self.root, "niwaki-2", NIWAKI_CONFIG)
        result = classify(path)
        self.assertFalse(result["capabilities"]["vision"])
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["runtimeFit"]["mlx-lm"], "unsupported")
        self.assertEqual(result["runtimeFit"]["mlx-vlm"], "supported")
        self.assertEqual(detect_engine(path), "mlx-vlm")

    def test_unreadable_inventory_keeps_the_config_only_answer(self):
        """No index and no readable header must not be read as "no vision"."""
        path = make_model(self.root, "unknown-inventory", VISION_CONFIG,
                          tensors=[], index=False)
        # A 8-byte stub is not a valid safetensors header, so the inventory is
        # unknown; the config-only answer (vision) is kept.
        result = classify(path)
        self.assertFalse(result["evidence"]["tensors"]["known"])
        self.assertTrue(result["capabilities"]["vision"])

    def test_worker_and_coordinator_agree_on_every_fixture(self):
        """AC-2-4: the worker's mirror of the classifier may not drift."""
        fixtures = {
            "text": (TEXT_CONFIG, ()),
            "vision": (VISION_CONFIG, (VISION_TENSOR,)),
            "niwaki": (NIWAKI_CONFIG, ()),
            "language-only": (dict(VISION_CONFIG, language_model_only=True),
                              (VISION_TENSOR,)),
        }
        for name, (config, tensors) in fixtures.items():
            path = make_model(self.root, name, config, tensors=tensors)
            expected = classify(path)["capabilities"]
            report = worker_vision.verify(expected, path)
            self.assertTrue(report["agree"], f"{name}: {report['differences']}")


class CatalogMigrationTests(unittest.TestCase):
    """AC-2-5: the four new columns are additive and old rows stay readable."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def _insert_one(self, database: Database) -> None:
        database.replace_models([{
            "id": "model-1", "source": "custom_folder", "name": "model-1",
            "path": str(self.root / "m"), "provider_key": None,
            "format": "mlx_lm", "engine": "mlx-lm", "modalities": ["text"],
            "confidence": 0.8, "reason": "test", "size_bytes": 1,
        }])

    def test_new_columns_are_added_to_an_existing_database(self):
        database = Database(self.db_path)
        self._insert_one(database)
        with database.connection:
            for column in ("capabilities", "runtimeFit", "evidence", "loaderId"):
                database.connection.execute(f"ALTER TABLE models DROP COLUMN {column}")
        database.close()

        reopened = Database(self.db_path)
        columns = {row["name"] for row in
                   reopened.connection.execute("PRAGMA table_info(models)")}
        self.assertTrue({"capabilities", "runtimeFit", "evidence", "loaderId"} <= columns)
        reopened.close()

    def test_rows_written_before_the_migration_decode_to_defaults(self):
        database = Database(self.db_path)
        self._insert_one(database)
        with database.connection:
            database.connection.execute(
                "UPDATE models SET capabilities=NULL, runtimeFit=NULL, "
                "evidence=NULL, loaderId=NULL WHERE id='model-1'")
        database.close()

        reopened = Database(self.db_path)
        row = reopened.get_model("model-1")
        self.assertEqual(row["capabilities"], {})
        self.assertEqual(row["runtimeFit"], {})
        self.assertEqual(row["evidence"], {})
        self.assertIsNone(row["loaderId"])
        self.assertEqual(row["modalities"], ["text"])
        reopened.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
