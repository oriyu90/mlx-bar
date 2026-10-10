"""Bulk presets / export-import / setup contracts (v2.6.0).

Covers DESIGN_v2.6.0.md sections 1-4: hardware-derived profiles always land
inside SettingsStore validation, effective-mode detection, leaf-by-leaf
import with skip reporting, fresh-install-only setup state, and the new
named CLI commands' request shapes (Mock client, same style as test_cli.py).
"""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock

from mlxbar import presets
from mlxbar.cli import execute
from mlxbar.presets import (MANAGED_KEYS, apply_payload, compute_profile,
                            effective_mode, profile_warnings, status_payload,
                            to_patch)
from mlxbar.settings import SettingsStore
from mlxbar.settings_export import (apply_import, build_export,
                                    EXPORT_FORMAT, EXPORT_FORMAT_VERSION)
from mlxbar.system_info import collect as collect_hardware
from mlxbar.system_info import default_max_resident_models


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def fresh_store(directory):
    return SettingsStore(root=Path(directory))


class HardwareCollectTests(unittest.TestCase):
    def test_collect_never_raises_and_reports_this_mac(self):
        hardware = collect_hardware()
        self.assertGreater(hardware["memory"]["physicalBytes"], 0)
        self.assertGreater(hardware["disk"]["freeBytes"], 0)
        self.assertIn("warnings", hardware)
        self.assertIn("defaultMaxResidentModels", hardware)
        self.assertIn("lmStudio", hardware)

    def test_default_resident_boundaries(self):
        self.assertEqual(default_max_resident_models(8), 1)
        self.assertEqual(default_max_resident_models(16), 1)
        self.assertEqual(default_max_resident_models(17), 2)
        self.assertEqual(default_max_resident_models(64), 2)
        self.assertEqual(default_max_resident_models(65), 3)


class ProfileComputeTests(unittest.TestCase):
    def test_modes_rejected(self):
        with self.assertRaises(ValueError):
            compute_profile("custom", 32, 200)

    def test_light_is_8bit_and_80_percent(self):
        profile = compute_profile("light", 96, 500)
        self.assertEqual(profile["experimental.kvCacheQuantization.mode"], "8bit")
        self.assertEqual(profile["models.pool.totalMemoryRatio"], 0.80)

    def test_full_is_bf16_and_slightly_larger_disks(self):
        light = compute_profile("full", 96, 500)
        small = compute_profile("light", 96, 500)
        self.assertEqual(light["experimental.kvCacheQuantization.mode"], "bf16")
        self.assertEqual(light["models.pool.totalMemoryRatio"], 0.85)
        self.assertGreater(light["promptCache.diskMaxGB"], small["promptCache.diskMaxGB"])
        self.assertGreater(light["promptCache.diskWriteBudgetGB"],
                           small["promptCache.diskWriteBudgetGB"])

    def test_tiny_and_huge_machines_stay_inside_validation(self):
        for physical_gb, free_gb in ((4, 20), (8, 60), (96, 168), (512, 4000)):
            for mode in ("light", "full"):
                with tempfile.TemporaryDirectory() as directory:
                    store = fresh_store(directory)
                    patch = to_patch(compute_profile(mode, physical_gb, free_gb))
                    store.update(patch)  # Must not raise: clamps hold.

    def test_low_disk_warns_but_still_applies(self):
        warnings = profile_warnings(20)
        self.assertTrue(any("ディスク" in warning for warning in warnings))
        self.assertTrue(any("再起動" in warning for warning in profile_warnings(500)))


class EffectiveModeTests(unittest.TestCase):
    def test_exact_match_reports_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            hardware = {"memory": {"physicalGB": 96}, "disk": {"freeGB": 500}}
            patch, _, _ = apply_payload("light", hardware)
            store.update(patch)
            status = status_payload(store.data, hardware)
            self.assertEqual(status["effective"], "light")
            self.assertEqual(status["stored"], "light")

    def test_any_tweak_flips_to_custom(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            hardware = {"memory": {"physicalGB": 96}, "disk": {"freeGB": 500}}
            patch, _, _ = apply_payload("full", hardware)
            store.update(patch)
            store.update({"models": {"pool": {"totalMemoryRatio": 0.75}}})
            status = status_payload(store.data, hardware)
            self.assertEqual(status["effective"], "custom")
            self.assertEqual(status["stored"], "full")

    def test_defaults_are_custom(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            hardware = {"memory": {"physicalGB": 96}, "disk": {"freeGB": 500}}
            self.assertEqual(effective_mode(store.data, 96, 500), "custom")

    def test_managed_keys_are_the_documented_six(self):
        self.assertEqual(len(MANAGED_KEYS), 6)


class PresetValidationTests(unittest.TestCase):
    def run_update(self, patch):
        with tempfile.TemporaryDirectory() as directory:
            return fresh_store(directory).update(patch)

    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            self.run_update({"presets": {"mode": "turbo"}})

    def test_custom_selection_is_a_noop_record(self):
        values = self.run_update({"presets": {"mode": "custom"}})
        self.assertEqual(values["presets"]["mode"], "custom")
        # Untouched managed keys keep their defaults.
        self.assertEqual(values["models"]["pool"]["totalMemoryRatio"], 0.75)

    def test_setup_fields_validated(self):
        with self.assertRaises(ValueError):
            self.run_update({"setup": {"completed": "yes"}})
        values = self.run_update({"setup": {"completed": True, "skipped": False,
                                             "completedAt": "2026-10-11T00:00:00+00:00",
                                             "version": "2.6.0"}})
        self.assertTrue(values["setup"]["completed"])

    def test_runtimes_validation_accepts_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            store.update({"runtimes": {"mlx-lm": {"autoCheck": True},
                                       "checkIntervalHours": 24, "keepSlots": 5}})
        with self.assertRaises(ValueError):
            self.run_update({"runtimes": {"mlx-lm": {"channel": "beta"}}})
        with self.assertRaises(ValueError):
            self.run_update({"runtimes": {"keepSlots": 101}})


class FreshInstallTests(unittest.TestCase):
    def test_new_directory_is_fresh_existing_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            first = fresh_store(directory)
            self.assertTrue(first.fresh_install)
            second = fresh_store(directory)
            self.assertFalse(second.fresh_install)


class ExportImportTests(unittest.TestCase):
    def test_envelope_shape_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            envelope = build_export(store.public(), collect_hardware())
        self.assertEqual(envelope["format"], EXPORT_FORMAT)
        self.assertEqual(envelope["formatVersion"], EXPORT_FORMAT_VERSION)
        self.assertIn("settings", envelope)
        self.assertIn("hardware", envelope)
        blob = json.dumps(envelope)
        self.assertNotIn("api-token", blob)

    def test_round_trip_applies_everything(self):
        with tempfile.TemporaryDirectory() as source_dir:
            source = fresh_store(source_dir)
            source.update({"general": {"language": "ja"},
                           "models": {"pool": {"maxResidentModels": 3}}})
            envelope = build_export(source.public(), collect_hardware())
        with tempfile.TemporaryDirectory() as target_dir:
            target = fresh_store(target_dir)
            result = apply_import(target, envelope, port_available=lambda port: True)
            self.assertIn("general.language", result["applied"])
            self.assertIn("models.pool.maxResidentModels", result["applied"])
            self.assertEqual(result["skipped"], [])
            self.assertEqual(target.data["general"]["language"], "ja")

    def test_future_schema_rejected_as_a_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            envelope = build_export(store.public(), collect_hardware())
            envelope["schemaVersion"] = 99
            with self.assertRaises(ValueError):
                apply_import(store, envelope)

    def test_wrong_format_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            with self.assertRaises(ValueError):
                apply_import(store, {"format": "nope"})

    def test_missing_roots_entry_skipped_others_kept(self):
        with tempfile.TemporaryDirectory() as source_dir, \
                tempfile.TemporaryDirectory() as keep_dir, \
                tempfile.TemporaryDirectory() as target_dir:
            source = fresh_store(source_dir)
            source.update({"models": {"roots": ["/nonexistent-models-xyz", keep_dir]}})
            envelope = build_export(source.public(), collect_hardware())
            target = fresh_store(target_dir)
            result = apply_import(target, envelope, port_available=lambda port: True)
            skipped_keys = [item["key"] for item in result["skipped"]]
            self.assertIn("models.roots", skipped_keys)
            # The surviving directory is kept even though its sibling vanished.
            self.assertEqual(target.data["models"]["roots"], [keep_dir])

    def test_port_in_use_skipped(self):
        with tempfile.TemporaryDirectory() as source_dir, \
                tempfile.TemporaryDirectory() as target_dir:
            source = fresh_store(source_dir)
            source.update({"api": {"port": 12000}})
            envelope = build_export(source.public(), collect_hardware())
            target = fresh_store(target_dir)
            result = apply_import(target, envelope, port_available=lambda port: False)
            skipped = {item["key"]: item["reason"] for item in result["skipped"]}
            self.assertIn("api.port", skipped)
            self.assertEqual(target.data["api"]["port"], 11435)

    def test_cross_field_half_change_skipped(self):
        # host without allowLan fails whole-config validation: the leaf is
        # skipped and the live pair stays consistent.
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            result = apply_import(
                store,
                {"format": EXPORT_FORMAT, "formatVersion": EXPORT_FORMAT_VERSION,
                 "schemaVersion": 1, "settings": {"api": {"host": "0.0.0.0"}}},
                port_available=lambda port: True)
            skipped = {item["key"]: item["reason"] for item in result["skipped"]}
            self.assertIn("api.host", skipped)
            self.assertEqual(store.data["api"]["host"], "127.0.0.1")

    def test_invalid_leaf_skipped_with_reason_rest_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            store = fresh_store(directory)
            result = apply_import(
                store,
                {"format": EXPORT_FORMAT, "formatVersion": EXPORT_FORMAT_VERSION,
                 "schemaVersion": 1,
                 "settings": {"general": {"language": "xx", "logLevel": "debug"}}},
                port_available=lambda port: True)
            self.assertIn("general.logLevel", result["applied"])
            skipped = {item["key"]: item["reason"] for item in result["skipped"]}
            self.assertIn("general.language", skipped)
            self.assertTrue(skipped["general.language"])
            self.assertEqual(store.data["general"]["logLevel"], "debug")


class NewCLITests(unittest.TestCase):
    def test_setup_scan(self):
        client = Mock()
        client.request.return_value = response({"memory": {}})
        args = argparse.Namespace(command="setup", action="scan")
        execute(args, client)
        client.request.assert_called_once_with("GET", "/api/v1/system/hardware")

    def test_setup_run_skip(self):
        client = Mock()
        client.request.return_value = response({"completed": True})
        args = argparse.Namespace(command="setup", action="run", skip=True,
                                  models_folder=None, max_resident=None,
                                  preset=None, non_interactive=True)
        execute(args, client)
        client.request.assert_called_once_with("POST", "/api/v1/setup/complete",
                                               {"skipped": True})

    def test_apply_preset_posts_mode(self):
        client = Mock()
        client.request.return_value = response({"mode": "light"})
        args = argparse.Namespace(command="config", action="apply-preset", mode="light",
                                  wait=False, no_scan=False, global_json=True)
        execute(args, client)
        client.request.assert_called_once_with("POST", "/api/v1/presets/apply",
                                               {"mode": "light"})

    def test_export_to_file(self):
        client = Mock()
        envelope = {"format": EXPORT_FORMAT, "appVersion": "2.6.0",
                    "exportedAt": "t", "settings": {}}
        client.request.return_value = response(envelope)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "settings.json")
            args = argparse.Namespace(command="config", action="export", file=path)
            result = execute(args, client)
            self.assertEqual(result["exported"], path)
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), envelope)

    def test_import_posts_envelope(self):
        client = Mock()
        client.request.return_value = response({"applied": [], "skipped": []})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"format": EXPORT_FORMAT}), encoding="utf-8")
            args = argparse.Namespace(command="config", action="import", file=str(path))
            execute(args, client)
        method, url, body = client.request.call_args[0]
        self.assertEqual((method, url), ("POST", "/api/v1/settings/import"))
        self.assertEqual(body, {"export": {"format": EXPORT_FORMAT}})

    def test_set_runtime_builds_patch(self):
        client = Mock()
        client.request.return_value = response({})
        args = argparse.Namespace(command="config", action="set-runtime",
                                  channel_mlx_lm=None, channel_mlx_vlm=None,
                                  auto_check_mlx_lm="true", auto_check_mlx_vlm=None,
                                  auto_install_missing=None,
                                  check_interval_hours=24, keep_slots=None)
        execute(args, client)
        client.request.assert_called_once_with("PUT", "/api/v1/settings", {
            "runtimes": {"mlx-lm": {"autoCheck": True}, "checkIntervalHours": 24}})

    def test_set_runtime_requires_an_option(self):
        client = Mock()
        args = argparse.Namespace(command="config", action="set-runtime",
                                  channel_mlx_lm=None, channel_mlx_vlm=None,
                                  auto_check_mlx_lm=None, auto_check_mlx_vlm=None,
                                  auto_install_missing=None,
                                  check_interval_hours=None, keep_slots=None)
        with self.assertRaises(ValueError):
            execute(args, client)

    def test_set_memory_limit_rewrites_profiles_keeping_fields(self):
        client = Mock()
        client.request.side_effect = [
            response({"models": {"pool": {"profiles": [
                {"modelId": "a", "keepLoaded": True, "replicas": 2},
                {"modelId": "b", "keepLoaded": False}]}}}),
            response({}),
        ]
        args = argparse.Namespace(command="model", action="set-memory-limit",
                                  model_id="a", gb=48)
        execute(args, client)
        _, _, body = client.request.call_args[0]
        profiles = body["models"]["pool"]["profiles"]
        self.assertEqual(profiles[0], {"modelId": "a", "keepLoaded": True,
                                       "replicas": 2, "maxMemoryGB": 48})
        self.assertEqual(profiles[1], {"modelId": "b", "keepLoaded": False})

    def test_set_memory_limit_range_checked(self):
        client = Mock()
        args = argparse.Namespace(command="model", action="set-memory-limit",
                                  model_id="a", gb=513)
        with self.assertRaises(ValueError):
            execute(args, client)

    def test_setup_status_hides_hardware(self):
        client = Mock()
        client.request.return_value = response({"required": False, "completed": True,
                                                "hardware": {"memory": {}}})
        args = argparse.Namespace(command="setup", action="status")
        result = execute(args, client)
        self.assertNotIn("hardware", result)


if __name__ == "__main__":
    unittest.main()
