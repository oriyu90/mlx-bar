from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mlxbar.catalog.scanner import scan_all
from mlxbar.database import Database
from mlxbar.settings import DEFAULTS


def make_mlx_model(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type":"test"}', encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").touch()


class CatalogDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_database_duplicates_are_detected_for_startup_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            model.mkdir()
            database = Database(root / "state.sqlite3")
            common = {"name": "model", "path": str(model), "provider_key": None,
                      "format": "mlx_lm", "engine": "mlx-lm", "modalities": ["text"],
                      "confidence": 0.8, "reason": "test", "size_bytes": 0}
            database.replace_models([
                {"id": "custom:one", "source": "custom_folder", **common},
                {"id": "lm:two", "source": "lm_studio_folder", **common},
            ])
            self.assertTrue(database.has_duplicate_model_paths())

    async def test_existing_pathless_provider_is_detected_for_startup_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "Laguna-S-2.1-oQ2e"
            model.mkdir()
            database = Database(root / "state.sqlite3")
            common = {"format": "mlx_lm", "modalities": ["text"], "confidence": 0.8,
                      "reason": "test", "size_bytes": 0}
            database.replace_models([
                {"id": "local:one", "source": "lm_studio_folder",
                 "name": "Laguna-S-2.1-oQ2e", "path": str(model),
                 "provider_key": None, "engine": "mlx-lm", **common},
                {"id": "api:two", "source": "lm_studio_api",
                 "name": "laguna-s-2.1-oq2e", "path": None,
                 "provider_key": "laguna-s-2.1-oq2e", "engine": "lm-studio", **common},
            ])
            self.assertTrue(database.has_mergeable_pathless_providers())

    async def test_catalog_classifier_version_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            self.assertIsNone(database.metadata_value("catalog_classifier_version"))
            database.set_metadata_value("catalog_classifier_version", "2")
            self.assertEqual(database.metadata_value("catalog_classifier_version"), "2")

    async def test_same_lm_studio_folder_added_as_custom_root_is_listed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lm_root = root / "lmstudio" / "models"
            model = lm_root / "publisher" / "duplicate-model"
            make_mlx_model(model)
            empty_hf = root / "empty-hf"
            empty_hf.mkdir()
            settings = deepcopy(DEFAULTS)
            settings["models"]["roots"] = [str(lm_root)]
            settings["models"]["lmStudio"].update({"folder": str(lm_root), "enabled": False})

            with patch.dict(os.environ, {"HF_HUB_CACHE": str(empty_hf)}):
                models = await scan_all(settings)

            self.assertEqual(len(models), 1)
            self.assertEqual(models[0]["path"], str(model.resolve()))
            self.assertEqual(models[0]["source"], "lm_studio_folder")

    async def test_provider_path_merges_with_same_local_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lm_root = root / "models"
            model = lm_root / "publisher" / "provider-model"
            make_mlx_model(model)
            empty_hf = root / "empty-hf"
            empty_hf.mkdir()
            settings = deepcopy(DEFAULTS)
            settings["models"]["lmStudio"].update({"folder": str(lm_root), "enabled": True})
            provider = {"provider_key": "publisher/provider-model", "path": str(model),
                        "compatibility_type": "mlx"}

            with patch.dict(os.environ, {"HF_HUB_CACHE": str(empty_hf)}), \
                 patch("mlxbar.catalog.scanner.cli_models", new=AsyncMock(return_value=[provider])), \
                 patch("mlxbar.catalog.scanner.api_models", new=AsyncMock(return_value=[
                     {"provider_key": "publisher/provider-model", "path": None,
                      "compatibility_type": None}
                 ])):
                models = await scan_all(settings)

            self.assertEqual(len(models), 1)
            self.assertEqual(models[0]["provider_key"], "publisher/provider-model")
            self.assertEqual(models[0]["engine"], "mlx-lm")

    async def test_pathless_provider_merges_with_one_case_insensitive_name_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lm_root = root / "models"
            model = lm_root / "publisher" / "Laguna-S-2.1-oQ2e"
            make_mlx_model(model)
            empty_hf = root / "empty-hf"
            empty_hf.mkdir()
            settings = deepcopy(DEFAULTS)
            settings["models"]["lmStudio"].update({"folder": str(lm_root), "enabled": True})

            with patch.dict(os.environ, {"HF_HUB_CACHE": str(empty_hf)}), \
                 patch("mlxbar.catalog.scanner.cli_models", new=AsyncMock(return_value=[])), \
                 patch("mlxbar.catalog.scanner.api_models", new=AsyncMock(return_value=[
                     {"provider_key": "laguna-s-2.1-oq2e", "path": None,
                      "compatibility_type": None}
                 ])):
                models = await scan_all(settings)

            self.assertEqual(len(models), 1)
            self.assertEqual(models[0]["provider_key"], "laguna-s-2.1-oq2e")
            self.assertEqual(models[0]["engine"], "mlx-lm")

    async def test_distinct_paths_with_same_name_are_not_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first" / "same-name"
            second = root / "second" / "same-name"
            make_mlx_model(first)
            make_mlx_model(second)
            empty_hf = root / "empty-hf"
            empty_hf.mkdir()
            empty_lm = root / "empty-lm"
            empty_lm.mkdir()
            settings = deepcopy(DEFAULTS)
            settings["models"]["roots"] = [str(first.parent), str(second.parent)]
            settings["models"]["lmStudio"].update({"folder": str(empty_lm), "enabled": False})

            with patch.dict(os.environ, {"HF_HUB_CACHE": str(empty_hf)}):
                models = await scan_all(settings)

            self.assertEqual(len(models), 2)
            self.assertEqual({item["path"] for item in models}, {str(first.resolve()), str(second.resolve())})


if __name__ == "__main__":
    unittest.main()
