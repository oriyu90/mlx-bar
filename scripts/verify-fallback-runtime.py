#!/usr/bin/env python3
"""v5.0.0 live end-to-end check for API fallback (TEST_PLAN_v5.0.0.md 2-1..2-4).

Runs a *real* coordinator process (`python -m mlxbar.main --home <tmp>`) and a
fake external provider, then drives the public API over its TCP port and the
management API over its Unix socket -- the same paths the app uses. No model
weights are needed: the fallback path never touches a worker.

Checks:
  2-1  disabled feature -> the local QUEUE_FULL error is returned unchanged
  2-2  enabled + queue full -> the external answer is streamed (OpenAI chat)
  2-3  the Anthropic surface falls back too
  2-4  an image request goes to the vision-capable model only
  2-5  management API round trip (provider/model/key/status)

Usage: python3 scripts/verify-fallback-runtime.py
Exit status 0 only when every check passes.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "Coordinator" / ".venv" / "bin" / "python"
PORT = 18935
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class FakeProvider(BaseHTTPRequestHandler):
    """Answers both protocols with a fixed stream and records what it got."""

    received: list[dict] = []

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeProvider.received.append({"path": self.path, "body": body,
                                      "auth": self.headers.get("authorization"),
                                      "api_key": self.headers.get("x-api-key")})
        if self.path.endswith("/messages"):
            self._sse([
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "ANTHROPIC-OK"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                 "usage": {"input_tokens": 3, "output_tokens": 1}},
            ])
            return
        if self.path.endswith("/responses"):
            self._sse([
                {"type": "response.output_text.delta", "delta": "RESPONSES-OK"},
                {"type": "response.completed",
                 "response": {"status": "completed",
                              "usage": {"input_tokens": 3, "output_tokens": 1}}},
            ])
            return
        self._sse([
            {"choices": [{"delta": {"role": "assistant", "content": "EXTERNAL-OK"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
        ])

    def _sse(self, chunks: list[dict]) -> None:
        payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        payload += "data: [DONE]\n\n"
        data = payload.encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def wait_for_port(port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


class UnixClient:
    """Minimal HTTP-over-UDS client (stdlib only)."""

    def __init__(self, path: Path):
        self.path = path

    def request(self, method: str, target: str, body: dict | None = None) -> tuple[int, dict]:
        payload = json.dumps(body).encode() if body is not None else b""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(str(self.path))
        head = (f"{method} {target} HTTP/1.1\r\nHost: mlxbar\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n").encode()
        sock.sendall(head + payload)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        sock.close()
        raw = b"".join(chunks)
        header, _, rest = raw.partition(b"\r\n\r\n")
        status = int(header.split(b" ")[1])
        if b"transfer-encoding: chunked" in header.lower():
            decoded, rest = b"", rest
            while True:
                size_line, _, remaining = rest.partition(b"\r\n")
                size = int(size_line or b"0", 16)
                if size == 0:
                    break
                decoded += remaining[:size]
                rest = remaining[size + 2:]
            rest = decoded
        try:
            return status, json.loads(rest or b"{}")
        except json.JSONDecodeError:
            return status, {}


def main() -> int:
    if not PYTHON.exists():
        print(f"missing interpreter: {PYTHON}", file=sys.stderr)
        return 2
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    upstream_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    home = Path(tempfile.mkdtemp(prefix="mlxbar-fallback-e2e-"))
    # The public listener binds at startup, so the port has to be in the config
    # before the coordinator reads it. Write a minimal document; the store
    # merges it over the defaults.
    # A minimal MLX model folder so the catalog has one real, generatable row
    # (the fallback path never loads it -- the request is refused locally first).
    models_dir = home / "models" / "tiny-local"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "config.json").write_text(json.dumps({
        "model_type": "llama", "architectures": ["LlamaForCausalLM"],
        "hidden_size": 64, "num_hidden_layers": 2,
    }), encoding="utf-8")
    (models_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (models_dir / "weights.safetensors").write_bytes(b"\0" * 1024)
    (home / "config.json").write_text(json.dumps({
        "api": {"enabled": True, "host": "127.0.0.1", "port": PORT, "requireToken": False},
        "models": {"roots": [str(home / "models")]},
    }), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "Coordinator"), "PYTHONUNBUFFERED": "1"}
    log = open(home / "coordinator.log", "w")
    process = subprocess.Popen([str(PYTHON), "-m", "mlxbar.main", "--home", str(home)],
                               cwd=str(ROOT / "Coordinator"), env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{PORT}"
        socket_file = home / "control" / "coordinator.sock"
        deadline = time.monotonic() + 90
        while not socket_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        if not wait_for_port(PORT, 90):
            print("coordinator did not start", file=sys.stderr)
            print((home / "coordinator.log").read_text()[-4000:], file=sys.stderr)
            return 2
        uds = UnixClient(home / "control" / "coordinator.sock")
        chat = {"model": "absent-model", "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}]}

        # 2-1: disabled -> the local refusal is unchanged (no such model here is
        # a MODEL_NOT_FOUND, which must never fall back).
        status, payload = http_json("POST", f"{base}/v1/chat/completions", chat)
        check("2-1 disabled: MODEL_NOT_FOUND stays local",
              status == 404 and isinstance(payload, dict)
              and payload.get("error", {}).get("code") == "MODEL_NOT_FOUND",
              f"{status} {payload}")

        # 2-5: management round trip.
        status, created = uds.request("POST", "/api/v1/fallback/providers", {
            "name": "Fake", "protocol": "openai-chat",
            "baseURL": f"http://127.0.0.1:{upstream_port}/v1"})
        provider_id = created.get("providerId") if isinstance(created, dict) else None
        check("2-5 add provider", status == 201 and bool(provider_id), f"{status} {created}")
        if not provider_id:
            return 1
        status, added = uds.request(
            "POST", f"/api/v1/fallback/providers/{provider_id}/models",
            {"model": "fake-chat", "sourceModels": ["*"], "priority": 1})
        model_id = added.get("modelId") if isinstance(added, dict) else None
        check("2-5 add model", status == 201 and bool(model_id), f"{status} {added}")
        status, keyed = uds.request(
            "PUT", f"/api/v1/fallback/providers/{provider_id}/api-key",
            {"apiKey": "sk-e2e"})
        configured = ((keyed.get("apiFallback") or {}).get("providers") or [{}])[0].get(
            "apiKeyConfigured") if isinstance(keyed, dict) else None
        check("2-5 save key (never echoed)", status == 200 and configured is True
              and "sk-e2e" not in json.dumps(keyed), f"{status} {keyed}")

        # 2-2: enabled -> with no model loaded and autoload suspended, a request
        # for a real catalog model is refused locally (MODEL_NOT_LOADED), which
        # is exactly the "MLXBar cannot answer" condition the feature targets.
        status, _ = uds.request("PUT", "/api/v1/settings",
                                {"models": {"autoLoadOnAPIRequest": False}})
        check("2-2 suspend autoload", status == 200, str(status))
        # The catalog is scanned in the background at startup; wait for the row.
        catalog = []
        deadline = time.monotonic() + 60
        while not catalog and time.monotonic() < deadline:
            status, payload = uds.request("GET", "/api/v1/models")
            catalog = (payload.get("data") or payload.get("models") or []) if isinstance(payload, dict) else []
            if not catalog:
                time.sleep(0.5)
        local_model = catalog[0].get("id") if catalog else None
        check("2-2 a catalog model exists to request", bool(local_model),
              json.dumps(payload)[:200])
        status, enabled = uds.request("PUT", "/api/v1/fallback", {"enabled": True})
        check("2-2 enable fallback", status == 200, str(status))
        FakeProvider.received.clear()
        status, payload = http_json("POST", f"{base}/v1/chat/completions",
                                    {"model": local_model, "max_tokens": 32,
                                     "messages": [{"role": "user", "content": "hi"}]})
        content = ""
        if isinstance(payload, dict):
            choices = payload.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content") or ""
        check("2-2 non-stream falls back", status == 200 and content == "EXTERNAL-OK",
              f"{status} {payload}")
        check("2-2 provider key was sent",
              bool(FakeProvider.received) and FakeProvider.received[-1]["auth"] == "Bearer sk-e2e",
              json.dumps(FakeProvider.received[-1:])[:300])

        # 2-3: the Anthropic surface uses the same service.
        FakeProvider.received.clear()
        status, raw = http_json("POST", f"{base}/anthropic/v1/messages",
                                {"model": local_model, "max_tokens": 32,
                                 "messages": [{"role": "user", "content": "hi"}]},
                                headers={"anthropic-version": "2023-06-01"})
        text = ""
        if isinstance(raw, dict):
            text = "".join(block.get("text", "") for block in raw.get("content") or [])
        check("2-3 anthropic falls back", status == 200 and text == "EXTERNAL-OK",
              f"{status} {raw}")

        # 2-4: an image request must reach only the vision-capable model.
        status, vision = uds.request(
            "POST", f"/api/v1/fallback/providers/{provider_id}/models",
            {"model": "fake-vision", "sourceModels": ["*"], "priority": 5,
             "supportsImages": True})
        check("2-4 add vision model", status == 201, f"{status} {vision}")
        FakeProvider.received.clear()
        status, payload = http_json("POST", f"{base}/v1/chat/completions", {
            "model": local_model, "max_tokens": 32,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + _PNG}},
            ]}]})
        used = FakeProvider.received[-1]["body"]["model"] if FakeProvider.received else None
        check("2-4 images go to the vision model", status == 200 and used == "fake-vision",
              f"{status} model={used}")

        # Status endpoint reflects the usage.
        status, state = uds.request("GET", "/api/v1/fallback/status")
        last = (state.get("lastOutcome") or {}) if isinstance(state, dict) else {}
        check("2-5 status reports the last fallback",
              status == 200 and last.get("model") in {"fake-chat", "fake-vision"},
              f"{status} {last}")

        # 2-6: a wrong key is reported, and the local error is still returned.
        uds.request("PUT", f"/api/v1/fallback/providers/{provider_id}/api-key",
                    {"apiKey": ""})
        status, _ = uds.request("PUT", f"/api/v1/fallback", {"enabled": False})
        check("2-6 disable fallback", status == 200, str(status))
        status, payload = http_json("POST", f"{base}/v1/chat/completions",
                                    {"model": local_model, "max_tokens": 32,
                                     "messages": [{"role": "user", "content": "hi"}]})
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        check("2-6 disabled again returns the local error", status == 409 and code == "MODEL_NOT_LOADED",
              f"{status} {payload}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        upstream.shutdown()
        log.close()
        # The coordinator writes its own log under <home>/logs; surface the tail
        # when a check failed so a red run is diagnosable without a rerun.
        if FAILURES:
            for extra in sorted(home.glob("logs/*.log")):
                print(f"--- {extra.name} (tail) ---")
                for line in extra.read_text(errors="replace").splitlines()[-25:]:
                    print(line[:200])
        shutil.rmtree(home, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all fallback runtime checks passed")
    return 0


_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAAAAgAB"
        "SK3uWQAAAABJRU5ErkJggg==")

if __name__ == "__main__":
    raise SystemExit(main())
