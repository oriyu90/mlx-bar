"""v3.2.0 (ISS-6 / WP-610, WP-611): memory admission order, estimate split, codes.

The reported symptom was `MEMORY_BUDGET_EXCEEDED: 現在の空きメモリでは安全余白を
残してロードできません` on a 96 GiB machine with 52 GiB free, for a 35 GiB model.
Two independent defects produced it:

* the OS-free-memory check ran *before* the eviction plan, so a resident model
  that could simply be reclaimed still caused a refusal;
* the estimate charged the whole safetensors file at 1.35x, although MLX mmaps
  those pages and the kernel can reclaim them (anonymous memory is the part
  that cannot be reclaimed).

These tests pin the order, the two-component estimate, and the split between
the refusal codes (AC-6-1, AC-6-2, AC-6-3, AC-6-4).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_model_pool import FakeWorker

from mlxbar.errors import MLXBarError
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


def model(name: str, size_gb: float) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


class MemoryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 0, "defaultPerModelMaxGB": 32,
            "idleTTLSeconds": 30,
        })
        FakeWorker.instances = []
        FakeWorker.load_gate = None
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self.pool = ModelPoolSupervisor(self.root, self.settings)
        self.pool._physical_memory = lambda: 96 * GIB
        self.pool._host_capacity = lambda: (60 * GIB, 0)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    # --- the estimate -------------------------------------------------------

    def test_default_admission_mode_is_strict(self):
        self.assertEqual(self.pool._admission_mode(), "strict")
        self.assertIn("memoryAdmission", self.settings.public()["models"]["pool"])

    def test_strict_and_balanced_charge_the_weights_differently(self):
        strict = self.pool._cold_estimate(model("m", 10))
        self.settings.data["models"]["pool"]["memoryAdmission"] = "balanced"
        balanced = self.pool._cold_estimate(model("m", 10))
        self.assertEqual(strict, int(10 * GIB * 1.35) + (512 << 20))
        self.assertEqual(balanced, int(10 * GIB * 1.15) + (512 << 20))
        self.assertLess(balanced, strict)

    def test_anonymous_part_is_the_unreclaimable_slice(self):
        anonymous = self.pool._anonymous_estimate(model("m", 10))
        self.assertEqual(anonymous, (512 << 20) + int(10 * GIB * 0.10))
        self.assertLess(anonymous, self.pool._cold_estimate(model("m", 10)))

    def test_per_model_default_follows_physical_memory(self):
        # defaultPerModelMaxGB is the shipped 32 -> derive 60% of RAM (AC-6-3).
        self.assertEqual(self.pool._per_model_limit(model("m", 1)), int(96 * GIB * 0.6))

    def test_explicit_pool_default_beats_the_physical_derivation(self):
        self.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 55
        self.assertEqual(self.pool._per_model_limit(model("m", 1)), int(55 * GIB))

    def test_profile_limit_beats_every_default(self):
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "m", "keepLoaded": True, "maxMemoryGB": 48},
        ]
        self.assertEqual(self.pool._per_model_limit(model("m", 1)), int(48 * GIB))

    # --- the order ----------------------------------------------------------

    async def test_headroom_is_judged_on_the_anonymous_part(self):
        """A 1 GiB model with 1.5 GiB free no longer fails on its 1.85 GiB
        estimate: only the anonymous share (0.6 GiB) has to fit (AC-6-1)."""
        self.pool._host_capacity = lambda: (int(1.5 * GIB), 0)
        reservation, evict = await self.pool._admit(model("m", 1), resident=True)
        self.assertEqual(evict, [])
        self.assertGreater(reservation, 0)

    async def test_headroom_refusal_has_its_own_code_with_numbers(self):
        self.settings.data["models"]["pool"]["minimumSystemReserveGB"] = 1
        self.pool._host_capacity = lambda: (int(0.1 * GIB), 0)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool._admit(model("m", 1), resident=True)
        self.assertEqual(raised.exception.code, "MEMORY_HEADROOM_INSUFFICIENT")
        self.assertIn("GB", raised.exception.message)

    async def test_global_budget_refusal_has_its_own_code(self):
        self.pool._global_budget = lambda: 1 * GIB
        with self.assertRaises(MLXBarError) as raised:
            await self.pool._admit(model("m", 6), resident=True)
        self.assertEqual(raised.exception.code, "MEMORY_BUDGET_TOTAL")

    async def test_eviction_is_planned_before_the_headroom_verdict(self):
        """The ISS-6 case: a resident, reclaimable model plus a small amount of
        actually-free memory must not refuse an otherwise sound load."""
        await self.pool.load(model("resident", 8), pin=False)
        self.pool._global_budget = lambda: 12 * GIB
        self.pool._host_capacity = lambda: (1 * GIB, 0)
        reservation, evict = await self.pool._admit(model("new", 8), resident=True)
        self.assertEqual([slot.model["id"] for slot in evict], ["resident"])
        self.assertGreater(reservation, 0)

    async def test_fixed_models_are_never_evicted_to_make_room(self):
        await self.pool.load(model("pinned", 8), pin=True)
        self.pool._global_budget = lambda: 12 * GIB
        self.pool._host_capacity = lambda: (60 * GIB, 0)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool._admit(model("new", 8), resident=True)
        self.assertEqual(raised.exception.code, "MEMORY_POOL_EXHAUSTED")
        self.assertIn("pinned", self.pool._slots)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
