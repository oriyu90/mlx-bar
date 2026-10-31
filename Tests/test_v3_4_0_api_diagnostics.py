"""v3.4.0 — API diagnostics for the operator report (2026-10-31).

Two complaints, both about being unable to tell failures apart from outside:

A. Every auth failure looked identical. A client that had cached a stale key got
   the same 401 as any other auth problem, `/v1/models` needed the token, and
   nothing exposed when the key had been issued -- so the operator needed a
   logging proxy to find out they were sending an old key.
B. A stalled model-volume read turned into a 60 s window in which requests
   failed with a code that reads like a permanent misconfiguration, and looked
   to the client exactly like an auth/outage problem.

The chosen fixes are diagnostic, not permissive: the 401 now carries a
non-secret `key_fingerprint` + `token_issued_at`, `GET /v1/auth/status` answers
"what key does this server expect?" without credentials, `/health` reports the
volume and resident state, and a stalled read now returns a *retryable* error
with `retry_after_ms`.

Deliberately NOT adopted (and pinned here so it stays deliberate):
``/v1/models`` without a token, and accepting a rotated token for a grace
period. Both would weaken a contract the e2e suite already enforces.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mlxbar import authtoken
from mlxbar.errors import MLXBarError
from mlxbar.main import make_public_app
from mlxbar.settings import SettingsStore

TOKEN = "operator-test-token-0123456789"


def fake_state(settings, *, loaded=(), suspended=False):
    workers = SimpleNamespace(
        loaded=(loaded[0] if loaded else None),
        loaded_models=lambda: list(loaded),
        _legacy=SimpleNamespace(volume_guard=SimpleNamespace(stalled=lambda: suspended)),
    )
    return SimpleNamespace(
        settings=settings,
        workers=workers,
        database=SimpleNamespace(list_models=lambda: []),
        started_at=time.time() - 42.0,
        preload_state={"state": "loaded"},
        volume_state=lambda: "stalled" if suspended else "ok",
    )


class AuthDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.set_api_token(TOKEN)
        self.client = TestClient(make_public_app(fake_state(self.settings)))

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_401_names_the_current_key_without_leaking_it(self):
        response = self.client.get("/v1/models", headers={"authorization": "Bearer stale"})
        self.assertEqual(response.status_code, 401)
        error = response.json()["error"]
        self.assertEqual(error["code"], "AUTHENTICATION_FAILED")
        self.assertEqual(error["key_fingerprint"], authtoken.fingerprint(TOKEN))
        self.assertEqual(len(error["key_fingerprint"]), authtoken.FINGERPRINT_LENGTH)
        # Never the secret, and never a prefix of it.
        self.assertNotIn(TOKEN[:8], error["key_fingerprint"])
        self.assertNotIn(TOKEN, response.text)
        self.assertGreater(error["token_issued_at"], 0)
        self.assertEqual(error["retry_after_ms"], 0)

    def test_a_client_can_compare_its_own_key_without_a_credential(self):
        status = self.client.get("/v1/auth/status")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertTrue(body["token_required"])
        self.assertEqual(body["key_fingerprint"], authtoken.fingerprint(TOKEN))
        self.assertGreater(body["issued_at"], 0)
        self.assertIn("version", body)
        self.assertNotIn(TOKEN, status.text)

    def test_a_correct_key_is_accepted(self):
        response = self.client.get("/v1/models", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200)

    def test_rotation_invalidates_the_old_key_immediately(self):
        # The contract the e2e suite pins: rotating revokes. The reporter's
        # grace-period suggestion is therefore not implemented.
        replacement = "rotated-token-9876543210abcdef"
        self.settings.set_api_token(replacement)
        client = TestClient(make_public_app(fake_state(self.settings)))
        self.assertEqual(
            client.get("/v1/models", headers={"authorization": f"Bearer {TOKEN}"}).status_code, 401)
        self.assertEqual(
            client.get("/v1/models", headers={"authorization": f"Bearer {replacement}"}).status_code,
            200)
        self.assertEqual(client.get("/v1/auth/status").json()["key_fingerprint"],
                         authtoken.fingerprint(replacement))

    def test_the_model_list_still_requires_a_token(self):
        # Pinned on purpose: the report asked for this endpoint to be open, and
        # the answer is no -- /v1/auth/status covers the same need.
        self.assertEqual(self.client.get("/v1/models").status_code, 401)

    def test_a_server_without_a_token_reports_no_fingerprint(self):
        self.settings.update({"api": {"requireToken": False}})
        client = TestClient(make_public_app(fake_state(self.settings)))
        self.assertEqual(client.get("/v1/models").status_code, 200)
        body = client.get("/v1/auth/status").json()
        self.assertFalse(body["token_required"])
        self.assertIsNone(body["key_fingerprint"])


class HealthDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = SettingsStore(Path(self.temporary.name))
        self.settings.set_api_token(TOKEN)

    def tearDown(self):
        self.temporary.cleanup()

    def test_health_reports_state_without_a_token(self):
        client = TestClient(make_public_app(fake_state(
            self.settings, loaded=[{"id": "m1"}, {"id": "m2"}])))
        body = client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["auth_required"])
        self.assertTrue(body["model_loaded"])
        self.assertEqual(body["resident_model_count"], 2)
        self.assertEqual(body["volume_state"], "ok")
        self.assertEqual(body["preload_state"], "loaded")
        self.assertGreaterEqual(body["uptime_seconds"], 42)
        self.assertIn("version", body)
        # State only: no model names, no paths.
        self.assertNotIn("m1", client.get("/health").text)

    def test_health_reports_a_stalled_volume(self):
        client = TestClient(make_public_app(fake_state(self.settings, suspended=True)))
        self.assertEqual(client.get("/health").json()["volume_state"], "stalled")

    def test_health_is_reachable_without_a_token_through_the_listener(self):
        # The ASGI wrapper's allow-list must include the same paths.
        from mlxbar import main as main_module

        self.assertIn("/health", main_module.UNAUTHENTICATED_PATHS)
        self.assertIn("/v1/auth/status", main_module.UNAUTHENTICATED_PATHS)


class VolumeErrorTests(unittest.IsolatedAsyncioTestCase):
    """Report B: a stalled read must be retryable and say how long to wait."""

    def make(self, timeout=0.05, cooldown=45.0):
        from mlxbar.io_guard import VolumeGuard

        return VolumeGuard(timeout=timeout, cooldown=cooldown)

    async def test_a_stalled_read_is_retryable_and_carries_a_delay(self):
        guard = self.make()
        with self.assertLogs("mlxbar.io_guard", level="WARNING"):
            with self.assertRaises(MLXBarError) as caught:
                await guard.read(time.sleep, 0.3, what="model config")
        error = caught.exception
        self.assertEqual(error.code, "MODEL_VOLUME_UNREADABLE")
        self.assertTrue(error.retryable)
        self.assertEqual(error.retry_after_ms, 45000)
        payload = error.as_dict()["error"]
        self.assertEqual(payload["retry_after_ms"], 45000)
        self.assertTrue(payload["retryable"])

    async def test_the_fast_fail_while_stalled_still_reports_a_wait(self):
        guard = self.make()
        with self.assertRaises(MLXBarError):
            await guard.read(time.sleep, 0.2)
        with self.assertRaises(MLXBarError) as caught:
            await guard.read(lambda: 1)
        self.assertGreater(caught.exception.retry_after_ms or 0, 0)

    async def test_a_missing_path_is_reported_as_detached(self):
        guard = self.make(cooldown=45.0)
        missing = "/nonexistent/model-root/org/model"
        with self.assertLogs("mlxbar.io_guard", level="WARNING"):
            with self.assertRaises(MLXBarError) as caught:
                await guard.read(time.sleep, 0.3, what="model config",
                                 volume_path=missing)
        self.assertEqual(caught.exception.code, "MODEL_VOLUME_DETACHED")
        self.assertIn("接続", caught.exception.message)

    async def test_a_present_but_stalling_path_stays_unreadable(self):
        guard = self.make()
        with self.assertRaises(MLXBarError) as caught:
            await guard.read(time.sleep, 0.3, what="model config",
                             volume_path=str(Path(__file__).parent))
        self.assertEqual(caught.exception.code, "MODEL_VOLUME_UNREADABLE")

    def test_retry_after_ms_is_absent_unless_set(self):
        self.assertNotIn("retry_after_ms", MLXBarError("X", "m").as_dict()["error"])


class MetadataCacheTests(unittest.IsolatedAsyncioTestCase):
    """A model's token limit and capabilities are read once per process."""

    async def test_the_second_read_hits_the_cache(self):
        from mlxbar.workers.supervisor import WorkerSupervisor

        calls = []

        def read(path):
            calls.append(path)
            return 8192

        supervisor = SimpleNamespace(_metadata_cache={})
        guard_reads = []

        async def guard_read(func, *args, **kwargs):
            guard_reads.append(args)
            return func(*args)

        supervisor.volume_guard = SimpleNamespace(read=guard_read)
        first = await WorkerSupervisor._cached_metadata(
            supervisor, "max_tokens", "/models/a", read, "/models/a", what="model config")
        second = await WorkerSupervisor._cached_metadata(
            supervisor, "max_tokens", "/models/a", read, "/models/a", what="model config")
        self.assertEqual(first, 8192)
        self.assertEqual(second, 8192)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(guard_reads), 1)

    async def test_a_different_path_is_read_separately(self):
        from mlxbar.workers.supervisor import WorkerSupervisor

        calls = []
        supervisor = SimpleNamespace(_metadata_cache={})

        async def guard_read(func, *args, **kwargs):
            calls.append(args)
            return func(*args)

        supervisor.volume_guard = SimpleNamespace(read=guard_read)
        await WorkerSupervisor._cached_metadata(
            supervisor, "max_tokens", "/a", lambda path: 1, "/a", what="model config")
        await WorkerSupervisor._cached_metadata(
            supervisor, "max_tokens", "/b", lambda path: 2, "/b", what="model config")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
