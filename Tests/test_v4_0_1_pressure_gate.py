"""v4.0.1 / WP-0: the memory-pressure admission gate (F-01).

v4.0.0 refused **every** load while macOS reported pressure level 2
(`if pressure >= 2`), even though `available` and `reserve` had just been
measured. Level 2 is a warning macOS reports routinely, so a machine with 55%
of 96 GiB free answered 503 to a 0.14 GB model -- and the user had no way to
force a load. Every other pressure check in this code base uses 4 (critical).

The contract these tests pin:

* level >= 4  -> refuse, unconditionally (critical is critical).
* level 2-3   -> admit **iff** `available - reserve >= estimate`.
* every refusal carries the measured `pressure`/`available`/`reserve`/
  `estimate` so the report is diagnosable instead of just "retryable: true".
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


class MemoryPressureGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            # 1 GiB reserve keeps `available - reserve` easy to reason about.
            "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 32,
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

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def test_warning_pressure_with_enough_headroom_admits_the_load(self):
        # The F-01 scenario: level 2 and plenty of free memory. v4.0.1 admits.
        self.pool._host_capacity = lambda: (60 * GIB, 2)
        result = await self.pool.load_for_api(model("small", 0.5))
        self.assertEqual(result["id"], "small")
        self.assertEqual(self.pool._slots["small"].state, "ready")

    async def test_level_three_is_still_only_a_warning(self):
        # 3 is "warning" too, not critical -- same headroom rule applies.
        self.pool._host_capacity = lambda: (60 * GIB, 3)
        result = await self.pool.load_for_api(model("small", 0.5))
        self.assertEqual(result["id"], "small")

    async def test_critical_pressure_refuses_even_with_headroom(self):
        # 4 is critical: the 60 GiB of free memory must not override it.
        self.pool._host_capacity = lambda: (60 * GIB, 4)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(model("small", 0.5))
        self.assertEqual(raised.exception.code, "MEMORY_PRESSURE")
        self.assertFalse(self.pool._slots)
        detail = raised.exception.detail
        self.assertEqual(detail["pressure"], 4)
        self.assertEqual(detail["available"], 60 * GIB)
        self.assertEqual(detail["reserve"], 1 * GIB)

    async def test_warning_pressure_without_headroom_refuses_and_reports_numbers(self):
        # 2 GiB free, 1 GiB reserve -> 1 GiB headroom, far below an 8 GiB model.
        self.pool._host_capacity = lambda: (2 * GIB, 2)
        target = model("big", 8)
        estimate = self.pool._cold_estimate(target)
        self.assertGreater(estimate, 1 * GIB)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(target)
        self.assertEqual(raised.exception.code, "MEMORY_PRESSURE")
        self.assertFalse(self.pool._slots)
        detail = raised.exception.detail
        self.assertEqual(detail["pressure"], 2)
        self.assertEqual(detail["available"], 2 * GIB)
        self.assertEqual(detail["reserve"], 1 * GIB)
        self.assertEqual(detail["estimate"], estimate)
        self.assertEqual(detail["headroom"], 1 * GIB)

    async def test_the_refusal_reaches_the_http_payload_as_detail(self):
        # The numbers have to survive as_dict()/HTTPException, otherwise the
        # whole point (diagnosability) is lost between the pool and the client.
        self.pool._host_capacity = lambda: (2 * GIB, 2)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(model("big", 8))
        payload = raised.exception.as_dict()["error"]
        self.assertEqual(payload["code"], "MEMORY_PRESSURE")
        self.assertEqual(set(payload["detail"]),
                         {"pressure", "available", "reserve", "estimate", "headroom"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
