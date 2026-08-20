from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from mlxbar.settings import SettingsStore
from mlxbar.runtimes.slots import SlotStore


class CoordinatorE2ETests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.process: subprocess.Popen | None = None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.process:
            if self.process.stdout: self.process.stdout.close()
            if self.process.stderr: self.process.stderr.close()
        self.temporary.cleanup()

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(self, port: int):
        store = SettingsStore(self.home)
        store.update({"api": {"port": port}})
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "Coordinator")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "mlxbar.main", "--home", str(self.home)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        socket_path = self.home / "control" / "coordinator.sock"
        for _ in range(100):
            if socket_path.exists():
                transport = httpx.HTTPTransport(uds=str(socket_path))
                try:
                    with httpx.Client(transport=transport, base_url="http://mlxbar") as client:
                        if client.get("/api/v1/health").status_code == 200:
                            return client, socket_path
                except httpx.HTTPError:
                    pass
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(f"Coordinator exited early:\n{stdout.decode()}\n{stderr.decode()}")
            time.sleep(0.05)
        self.fail("Coordinator health timeout")

    def client(self, socket_path: Path):
        return httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), base_url="http://mlxbar")

    def test_management_public_auth_and_port_switch(self):
        old_port, new_port = self.free_port(), self.free_port()
        _, socket_path = self.start(old_port)
        with self.client(socket_path) as client:
            status = client.get("/api/v1/status").json()
            self.assertEqual(status["service"], "running")
            self.assertIsNone(status["api"]["error"])
            with client.stream("POST", "/api/v1/generate", json={"prompt": "not loaded"}) as response:
                events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
            self.assertEqual(events[0]["type"], "error")
            self.assertEqual(events[0]["code"], "MODEL_NOT_LOADED")
            token = (self.home / "control" / "api-token").read_text().strip()
            self.assertEqual(httpx.get(f"http://127.0.0.1:{old_port}/v1/models").status_code, 401)
            authorized = httpx.get(f"http://127.0.0.1:{old_port}/v1/models",
                                   headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(authorized.status_code, 200)
            replacement = "replacement-api-key-123456"
            self.assertEqual(client.put("/api/v1/settings/api-token", json={"token": replacement}).status_code, 200)
            self.assertEqual(httpx.get(f"http://127.0.0.1:{old_port}/v1/models",
                                       headers={"Authorization": f"Bearer {token}"}).status_code, 401)
            self.assertEqual(httpx.get(f"http://127.0.0.1:{old_port}/v1/models",
                                       headers={"Authorization": f"Bearer {replacement}"}).status_code, 200)
            self.assertEqual(client.put("/api/v1/settings", json={"api": {"requireToken": False}}).status_code, 200)
            self.assertEqual(httpx.get(f"http://127.0.0.1:{old_port}/v1/models").status_code, 200)
            changed = client.put("/api/v1/settings", json={"api": {"port": new_port}})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(httpx.get(f"http://127.0.0.1:{new_port}/health").status_code, 200)

    def test_lan_listener_switch_keeps_token_required(self):
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ) if entry[4][0] != "127.0.0.1" and not entry[4][0].startswith("169.254.")})
        if not addresses:
            self.skipTest("non-loopback IPv4 address is unavailable")
        port = self.free_port()
        _, socket_path = self.start(port)
        token = (self.home / "control" / "api-token").read_text().strip()
        with self.client(socket_path) as client:
            enabled = client.put("/api/v1/settings", json={
                "api": {"host": "0.0.0.0", "requireToken": True},
                "security": {"allowLan": True},
            })
            self.assertEqual(enabled.status_code, 200, enabled.text)
            status = client.get("/api/v1/status").json()
            self.assertTrue(status["api"]["lanEnabled"])
            self.assertEqual(status["api"]["host"], "0.0.0.0")
            self.assertIn(f"http://{addresses[0]}:{port}", status["api"]["lanUrls"])
            self.assertEqual(httpx.get(f"http://{addresses[0]}:{port}/v1/models").status_code, 401)
            # Authentication is listener-independent. Keep the credential on
            # loopback while the LAN address verifies external reachability.
            authorized = httpx.get(f"http://127.0.0.1:{port}/v1/models",
                                   headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(authorized.status_code, 200)
            rejected = client.put("/api/v1/settings", json={"api": {"requireToken": False}})
            self.assertEqual(rejected.status_code, 422)
            disabled = client.put("/api/v1/settings", json={
                "api": {"host": "127.0.0.1", "requireToken": True},
                "security": {"allowLan": False},
            })
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertFalse(client.get("/api/v1/status").json()["api"]["lanEnabled"])

    def test_port_conflict_keeps_management_available(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0)); blocker.listen()
        port = blocker.getsockname()[1]
        try:
            _, socket_path = self.start(port)
            with self.client(socket_path) as client:
                status = client.get("/api/v1/status").json()
                self.assertEqual(status["service"], "running")
                self.assertIn("使用できません", status["api"]["error"])
        finally:
            blocker.close()

    def test_failed_live_listener_switch_restores_previous_port(self):
        old_port = self.free_port()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0)); blocker.listen()
        blocked_port = blocker.getsockname()[1]
        try:
            _, socket_path = self.start(old_port)
            with self.client(socket_path) as client:
                changed = client.put("/api/v1/settings", json={"api": {"port": blocked_port}})
                self.assertEqual(changed.status_code, 409)
                self.assertEqual(client.get("/api/v1/settings").json()["api"]["port"], old_port)
                self.assertEqual(httpx.get(f"http://127.0.0.1:{old_port}/health").status_code, 200)
        finally:
            blocker.close()

    def test_inactive_runtime_slot_can_be_deleted_but_active_slot_is_protected(self):
        slots = SlotStore(self.home)
        root = slots.engine_root("mlx-lm") / "slots"
        for slot_id in ("old", "current"):
            slot = root / slot_id
            slot.mkdir()
            (slot / "probe.json").write_text(json.dumps({"compatible": True, "version": slot_id}))
        slots.activate("mlx-lm", "old")
        slots.activate("mlx-lm", "current")
        _, socket_path = self.start(self.free_port())
        with self.client(socket_path) as client:
            protected = client.delete("/api/v1/runtimes/mlx-lm/slots/current")
            self.assertEqual(protected.status_code, 409)
            deleted = client.delete("/api/v1/runtimes/mlx-lm/slots/old")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            self.assertTrue(deleted.json()["removedPrevious"])
            runtime = client.get("/api/v1/runtimes").json()["mlx-lm"]
            self.assertEqual(runtime["active"], {"active": "current", "previous": None})
            self.assertEqual(runtime["history"][0]["action"], "deleted")

    def test_lmstudio_streaming_route(self):
        received: dict = {}

        class ProviderHandler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self):
                received["get_authorization"] = self.headers.get("Authorization")
                body = json.dumps({"data": [{"id": "test-provider-model"}]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                received["post_authorization"] = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length", "0"))
                received.update(json.loads(self.rfile.read(length)))
                if self.path == "/api/v1/models/load":
                    body = json.dumps({"type": "llm", "instance_id": "test-provider-model",
                                       "status": "loaded", "load_time_seconds": 0.01}).encode()
                    self.send_response(200); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                for text in ("こん", "にちは"):
                    chunk = {"choices": [{"delta": {"content": text}}]}
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
        thread = threading.Thread(target=provider.serve_forever, daemon=True); thread.start()
        try:
            store = SettingsStore(self.home)
            store.update({"api": {"port": self.free_port()}, "models": {"lmStudio": {
                "baseUrl": f"http://127.0.0.1:{provider.server_port}"}}})
            store.set_lm_studio_token("lm-studio-test-token")
            _, socket_path = self.start(store.data["api"]["port"])
            with self.client(socket_path) as client:
                job = client.post("/api/v1/models/scan").json()
                for _ in range(100):
                    job = client.get(f"/api/v1/jobs/{job['id']}").json()
                    if job["state"] == "completed": break
                    time.sleep(0.05)
                models = client.get("/api/v1/models").json()["data"]
                model = next(item for item in models if item.get("provider_key") == "test-provider-model")
                self.assertEqual(client.post(f"/api/v1/models/{model['id']}/load", json={"engine": "lm-studio"}).status_code, 200)
                with client.stream("POST", "/api/v1/generate", json={"prompt": "テスト", "temperature": 0.2,
                                   "max_tokens": 17, "requestId": "integration-request"}) as response:
                    text = "".join(json.loads(line[6:]).get("text", "") for line in response.iter_lines()
                                   if line.startswith("data: "))
                self.assertEqual(text, "こんにちは")
                self.assertEqual(received["temperature"], 0.2)
                self.assertEqual(received["top_p"], 1.0)
                self.assertEqual(received["max_tokens"], 17)
                self.assertEqual(received["get_authorization"], "Bearer lm-studio-test-token")
                self.assertEqual(received["post_authorization"], "Bearer lm-studio-test-token")
        finally:
            provider.shutdown(); provider.server_close(); thread.join(timeout=2)

    def test_system_reset_wipes_root_and_shuts_down(self):
        port = self.free_port()
        _, socket_path = self.start(port)
        marker = self.home / "marker.txt"
        marker.write_text("should be gone", encoding="utf-8")
        with self.client(socket_path) as client:
            response = client.post("/api/v1/system/reset")
            self.assertIn(response.status_code, (200, 202))
            self.assertEqual(response.json().get("status"), "resetting")
        # The process tears itself down shortly after responding -- proves
        # the response really was sent before teardown, since httpx above
        # would have raised if the connection died mid-response.
        self.process.wait(timeout=10)
        self.assertIsNotNone(self.process.returncode)
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / "state.sqlite3").exists())
        self.assertFalse((self.home / "control" / "coordinator.sock").exists())
        self.assertFalse((self.home / "control" / "api-token").exists())

    def test_system_reset_cancels_active_jobs(self):
        port = self.free_port()
        _, socket_path = self.start(port)
        with self.client(socket_path) as client:
            job = client.post("/api/v1/models/scan").json()
            self.assertIn(job.get("state"), {"queued", "running", "completed"})
            response = client.post("/api/v1/system/reset")
            self.assertIn(response.status_code, (200, 202))
        # Reset must not hang waiting on the in-flight job -- if cancel_all()
        # ever deadlocks, this wait() times out and fails the test.
        self.process.wait(timeout=10)
        self.assertIsNotNone(self.process.returncode)


if __name__ == "__main__":
    unittest.main()
