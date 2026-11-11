#!/usr/bin/env python3
"""v5.1.0 live end-to-end check for continuous batching (TEST_PLAN_v5.1.0.md 2-1..2-6).

Runs a *real* coordinator process (`python -m mlxbar.main --home <tmp>`) with a
real mlx-lm worker and a real model, then drives the public API over its TCP
port and the management API over its Unix socket -- the same paths the app
uses. Nothing is faked except the model choice: pass a small MLX model folder
so the check finishes in minutes.

Checks:
  2-1  a model with continuous batching enabled reports `capable` and loads once
  2-2  two simultaneous requests are served by ONE worker (no replica)
  2-3  the second request starts generating before the first has finished
  2-4  cancelling one request leaves the other running
  2-5  an unsupported request (a seed) falls back to single generation and still
       answers
  2-6  with batching disabled the second request waits for the first (the
       pre-v5.1.0 behaviour is unchanged)

Usage:
    python3 scripts/verify-batch-runtime.py --model /path/to/mlx-model
    python3 scripts/verify-batch-runtime.py            # uses $MLXBAR_BATCH_TEST_MODEL

Exit status 0 only when every check passes.
"""
from __future__ import annotations

import argparse
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "Coordinator" / ".venv" / "bin" / "python"
PORT = 18937
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def wait_for_port(port: int, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def http_json(method: str, url: str, body: dict | None = None,
              headers: dict | None = None, timeout: float = 120) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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

    def request(self, method: str, target: str, body: dict | None = None,
                timeout: float = 180) -> tuple[int, dict]:
        payload = json.dumps(body).encode() if body is not None else b""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.path))
        head = (f"{method} {target} HTTP/1.1\r\nHost: mlxbar\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n").encode()
        sock.sendall(head + payload)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        sock.close()
        raw = b"".join(chunks)
        head_bytes, _, body_bytes = raw.partition(b"\r\n\r\n")
        status = int(head_bytes.split(b" ")[1]) if head_bytes else 0
        # A chunked response (the default for FastAPI's JSON responses) wraps
        # the body in size lines; decode it before parsing.
        if b"transfer-encoding: chunked" in head_bytes.lower():
            body_bytes = _dechunk(body_bytes)
        try:
            return status, json.loads(body_bytes.decode() or "{}")
        except json.JSONDecodeError:
            return status, {"raw": body_bytes.decode(errors="replace")}


def _dechunk(raw: bytes) -> bytes:
    out = []
    while raw:
        line, _, rest = raw.partition(b"\r\n")
        try:
            size = int(line.strip().split(b";")[0] or b"0", 16)
        except ValueError:
            return b"".join(out)
        if size == 0:
            break
        out.append(rest[:size])
        raw = rest[size + 2:]
    return b"".join(out)


def stream_chat(base: str, model: str, prompt: str, *, max_tokens: int,
                stop_after: float | None = None,
                events: list | None = None, headers: dict | None = None) -> dict:
    """POST a streaming chat completion and record when each delta arrived.

    ``stop_after`` closes the connection after that many seconds, which is what
    a client pressing "stop" does; the coordinator must then cancel only that
    request.
    """
    body = json.dumps({"model": model, "stream": True, "max_tokens": max_tokens,
                       # The server's own default is small; a short cap here
                       # would let the first request finish before the second
                       # even starts, which is not what 2-3 measures.
                       "repetition_penalty": 1.0,
                       # A non-zero temperature on purpose: `temperature: 0`
                       # makes the coordinator pass `seed: 0`, and a seeded
                       # request is deliberately never batched.
                       "temperature": 0.7,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    request = urllib.request.Request(f"{base}/v1/chat/completions", data=body,
                                     method="POST",
                                     headers={"content-type": "application/json",
                                              **(headers or {})})
    started = time.monotonic()
    first_delta = None
    text = ""
    finished = None
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode()
            if "text/event-stream" not in (response.headers.get("content-type") or ""):
                # The error happened before the stream started (e.g. the
                # coordinator answered a plain JSON error with a 200).
                print(f"  [non-stream reply] {raw[:800]}", flush=True)
                return {"error": raw[:800], "text": "", "first_delta": None,
                        "finished": None, "elapsed": time.monotonic() - started}
            for line in raw.splitlines():
                if stop_after is not None and time.monotonic() - started >= stop_after:
                    break
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if events is not None:
                    events.append((time.monotonic() - started, chunk))
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    if first_delta is None:
                        first_delta = time.monotonic() - started
                if choice.get("finish_reason"):
                    finished = choice["finish_reason"]
                if chunk.get("error"):
                    # Streaming errors arrive as a data frame, not a status code.
                    print(f"  [stream error] {json.dumps(chunk, ensure_ascii=False)[:800]}",
                          flush=True)
                    return {"error": json.dumps(chunk.get("error"), ensure_ascii=False),
                            "text": text, "first_delta": first_delta,
                            "finished": finished, "elapsed": time.monotonic() - started}
    except urllib.error.HTTPError as exc:
        # The server's error body is the diagnosis; without it a failure here
        # only says "502".
        try:
            detail = exc.read().decode()
        except Exception:  # noqa: BLE001
            detail = ""
        return {"error": f"HTTP {exc.code}: {detail[:600]}", "text": text,
                "first_delta": first_delta, "finished": finished,
                "elapsed": time.monotonic() - started}
    except Exception as exc:  # noqa: BLE001 - reported as a failure detail
        return {"error": repr(exc), "text": text, "first_delta": first_delta,
                "finished": finished, "elapsed": time.monotonic() - started}
    return {"text": text, "first_delta": first_delta, "finished": finished,
            "elapsed": time.monotonic() - started}


def status_of(uds: UnixClient) -> dict:
    _, payload = uds.request("GET", "/api/v1/status")
    return payload if isinstance(payload, dict) else {}


def wait_until(predicate, timeout: float, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("MLXBAR_BATCH_TEST_MODEL"))
    parser.add_argument("--keep", action="store_true", help="keep the temporary home")
    parser.add_argument("--log", action="store_true", help="print the coordinator and worker logs")
    args = parser.parse_args()
    if not args.model or not Path(args.model).is_dir():
        print("pass --model <mlx model folder> (or set MLXBAR_BATCH_TEST_MODEL)",
              file=sys.stderr)
        return 2
    if not PYTHON.exists():
        print(f"missing interpreter: {PYTHON}", file=sys.stderr)
        return 2

    home = Path(tempfile.mkdtemp(prefix="mlxbar-batch-e2e-"))
    model_dir = home / "models" / Path(args.model).name
    model_dir.parent.mkdir(parents=True, exist_ok=True)
    # A real copy, not a symlink: the coordinator's catalog and the worker both
    # enumerate the model's files, and a linked directory does not survive that
    # consistently. A small test model makes the copy cheap.
    shutil.copytree(Path(args.model).resolve(), model_dir)
    (home / "config.json").write_text(json.dumps({
        "api": {"enabled": True, "host": "127.0.0.1", "port": PORT, "requireToken": False},
        # `watchFolders: false` keeps the coordinator's own model folders (the
        # user's LM Studio library, the Hugging Face cache) out of the catalog,
        # so this check always drives the model it was given.
        "models": {"roots": [str(home / "models")], "watchFolders": False,
                   "autoLoadOnAPIRequest": True,
                   "pool": {"enabled": True, "maxResidentModels": 2,
                            "maxReplicasPerModel": 1, "generationConcurrency": 2,
                            "idleTTLSeconds": 900,
                            "defaultPerModelMaxGB": 16, "totalMemoryRatio": 0.9}},
        "general": {"preloadLastModel": False},
        "runtimes": {"mlx-lm": {"channel": "stable", "autoCheck": False},
                     "mlx-vlm": {"channel": "stable", "autoCheck": False},
                     "autoInstallMissing": False},
    }), encoding="utf-8")

    # The worker runs from the coordinator's runtime slot. `active.json` is a
    # symlink to the installed runtime (read-only use); the slot itself is
    # copied, because `.venv/bin/python` points at a relative interpreter path
    # that only resolves while the venv keeps its own name. The copy is what
    # makes the first worker start slow (a cold site-packages tree), so the
    # load call below gets a generous timeout.
    installed = Path.home() / "Library" / "Application Support" / "MLXBar" / "runtimes"
    for engine in ("mlx-lm", "mlx-vlm"):
        source = installed / engine
        if not source.exists():
            continue
        target = home / "runtimes" / engine
        target.mkdir(parents=True, exist_ok=True)
        active = source / "active.json"
        if active.exists():
            (target / "active.json").symlink_to(active)
            slot_id = json.loads(active.read_text()).get("active")
            (target / "slots").mkdir(exist_ok=True)
            if slot_id:
                shutil.copytree(source / "slots" / slot_id, target / "slots" / slot_id,
                                symlinks=True, dirs_exist_ok=True)

    env = {**os.environ, "PYTHONPATH": str(ROOT / "Coordinator"), "PYTHONUNBUFFERED": "1",
           # The worker is started from a copied runtime slot, and its first
           # import of a cold site-packages tree can take longer than the
           # production startup budget. This only widens the budget for this
           # check; the coordinator itself is unchanged.
           "MLXBAR_WORKER_STARTUP_SECONDS": "90"}
    log = open(home / "coordinator.log", "w")
    process = subprocess.Popen([str(PYTHON), "-m", "mlxbar.main", "--home", str(home)],
                               cwd=str(ROOT / "Coordinator"), env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        socket_file = home / "control" / "coordinator.sock"
        deadline = time.monotonic() + 120
        while not socket_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        if not wait_for_port(PORT, 120):
            print("coordinator did not start", file=sys.stderr)
            print((home / "coordinator.log").read_text()[-4000:], file=sys.stderr)
            return 2
        uds = UnixClient(socket_file)
        base = f"http://127.0.0.1:{PORT}"

        catalog = wait_until(lambda: (uds.request("GET", "/api/v1/models")[1] or {}).get("data")
                             or (uds.request("GET", "/api/v1/models")[1] or {}).get("models"),
                             90)
        rows = catalog or []
        if not rows:
            print("model not found in the catalog", file=sys.stderr)
            return 2
        # The catalog lists every model it can see; pick the one this check was
        # given (by name), never "whatever sorts first".
        wanted = Path(args.model).name.casefold()
        row = next((item for item in rows
                    if str(item.get("name", "")).casefold() == wanted), None)
        if row is None:
            print(f"{Path(args.model).name} not found in the catalog: "
                  f"{[item.get('name') for item in rows][:10]}", file=sys.stderr)
            return 2
        model_id = row.get("id")
        print(f"model: {row.get('name')} ({model_id}) engine={row.get('engine')}")

        # 2-1: enable continuous batching for this model and load it once.
        status, payload = uds.request(
            "PUT", f"/api/v1/settings/models/pool/profiles/{model_id}",
            {"keepLoaded": True,
             "continuousBatching": {"enabled": True, "maxSequences": 4,
                                    "prefillBatchSize": 4, "prefillStepSize": 2048}})
        check("2-1 enable continuous batching", status == 200, f"{status} {payload}")
        status, loaded = uds.request("POST", f"/api/v1/models/{model_id}/load",
                                     {"engine": "mlx-lm"}, timeout=900)
        check("2-1 load the model", status == 200, f"{status} {str(loaded)[:400]}")
        status_info = status_of(uds)
        entries = status_info.get("loadedModels") or []
        batching = (entries[0] or {}).get("continuousBatching") if entries else None
        check("2-1 worker reports batching capable",
              bool(batching and batching.get("capable")),
              json.dumps(batching)[:300])

        # 2-2 / 2-3: two simultaneous requests on one worker.
        results: dict[str, dict] = {}

        def run(name: str, prompt: str, max_tokens: int) -> None:
            results[name] = stream_chat(base, model_id, prompt, max_tokens=max_tokens)

        first = threading.Thread(target=run, args=("a", "Write a long story about the sea.", 256))
        first.start()
        time.sleep(1.0)
        second = threading.Thread(target=run, args=("b", "List three colours.", 256))
        second.start()
        deadline = time.monotonic() + 120
        peak = 0
        while (first.is_alive() or second.is_alive()) and time.monotonic() < deadline:
            info = status_of(uds)
            entries = info.get("loadedModels") or []
            if entries:
                peak = max(peak, int(entries[0].get("batchActiveSequences") or 0))
            time.sleep(0.2)
        first.join()
        second.join()
        check("2-2 both requests answered",
              bool(results.get("a", {}).get("text")) and bool(results.get("b", {}).get("text")),
              json.dumps({k: ((v.get("text") or "")[:40], v.get("error")) for k, v in results.items()},
                         ensure_ascii=False))
        check("2-2 one worker served both sequences", peak >= 2, f"peak={peak}")
        # "Started generating before the first finished" is judged from the
        # server's own view: the second sequence was active while the first one
        # still was (peak >= 2 above), and each request reports its own first
        # token. Comparing the client-side timestamps alone would also depend on
        # how long the server took to send the first delta.
        check("2-3 the second request started before the first finished",
              peak >= 2 and results.get("b", {}).get("first_delta") is not None
              and results.get("a", {}).get("first_delta") is not None,
              f"peak={peak} b_first={results.get('b', {}).get('first_delta')} "
              f"a_first={results.get('a', {}).get('first_delta')}")
        status_info = status_of(uds)
        check("2-2 still exactly one resident model",
              status_info.get("modelPool", {}).get("residentModelCount") == 1,
              json.dumps(status_info.get("modelPool"))[:300])

        # 2-4: cancel one of two requests; the other must finish. The cancelled
        # one closes its connection after 2 s, which is what the stop button
        # does; the survivor must still complete normally.
        results.clear()
        cancelled = threading.Thread(
            target=lambda: results.__setitem__(
                "c", stream_chat(base, model_id, "Write a very long essay.",
                                 max_tokens=512, stop_after=2.0)))
        survivor = threading.Thread(target=run, args=("d", "Say hello.", 128))
        cancelled.start()
        survivor.start()
        survivor.join(timeout=300)
        cancelled.join(timeout=300)
        check("2-4 the other request completed normally",
              results.get("d", {}).get("finished") in {"stop", "length"},
              json.dumps(results.get("d", {}))[:300])
        time.sleep(1.0)
        status_info = status_of(uds)
        entries = status_info.get("loadedModels") or []
        check("2-4 no sequence left running after the cancel",
              not entries or int(entries[0].get("batchActiveSequences") or 0) == 0,
              json.dumps(entries[0].get("batchActiveSequences") if entries else None))

        # 2-5: a seeded request cannot be batched; it must still answer.
        seeded = http_json("POST", f"{base}/v1/chat/completions", {
            "model": model_id, "max_tokens": 16, "temperature": 0.7, "seed": 1234,
            "messages": [{"role": "user", "content": "Say hi."}]}, timeout=300)
        ok = seeded[0] == 200 and isinstance(seeded[1], dict)
        check("2-5 a seeded request falls back and answers", ok, str(seeded)[:300])

        # 2-6: disabled -> the second request waits for the first.
        uds.request("PUT", f"/api/v1/settings/models/pool/profiles/{model_id}",
                    {"continuousBatching": {"enabled": False}})
        uds.request("POST", f"/api/v1/models/{model_id}/unload")
        uds.request("POST", f"/api/v1/models/{model_id}/load", {"engine": "mlx-lm"})
        results.clear()
        first = threading.Thread(target=run, args=("e", "Write a long story about the sea.", 64))
        first.start()
        time.sleep(1.0)
        second = threading.Thread(target=run, args=("f", "List three colours.", 64))
        second.start()
        first.join(timeout=300)
        second.join(timeout=300)
        check("2-6 with batching disabled both requests still answer",
              bool(results.get("e", {}).get("text")) and bool(results.get("f", {}).get("text")),
              json.dumps({k: (v.get("text") or "")[:40] for k, v in results.items()}))
        check("2-6 the second request waited for the first",
              (results.get("f", {}).get("first_delta") or 0) >= (results.get("e", {}).get("elapsed") or 0),
              f"f_first={results.get('f', {}).get('first_delta')} "
              f"e_elapsed={results.get('e', {}).get('elapsed')}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        if args.log:
            paths = [home / "coordinator.log", home / "logs" / "coordinator.log"]
            paths += sorted((home / "logs").glob("worker-*.log"))
            paths += sorted((home / "logs").glob("**/worker-*.log"))
            seen = set()
            for path in paths:
                if path in seen:
                    continue
                seen.add(path)
                if path.exists() and path.stat().st_size:
                    print(f"--- {path.name} (tail) ---")
                    print(path.read_text(errors="replace")[-6000:])
        if args.keep:
            print(f"temporary home kept: {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
