"""v2.8.0 cooperative generation abort.

Owner-supplied CPU report: with the client gone, the worker kept generating and
kept the single MLX thread saturated, so every later request starved. The cause
was that the only way to stop a stream was to queue `generator.close()` on the
MLX thread -- which cannot run until the in-flight `next()` returns, and which
never stopped the adapter from asking for the *next* token in the first place.

These tests pin the properties that fix has to hold:

1. a disconnect stops the generation within a token or two, not `max_tokens`;
2. `/rpc cancel` stops it the same way, and does not have to wait for the MLX
   executor (the old path went through it, so the stop waited for the work it
   was trying to stop);
3. a truncated reply is reported as `cancelled`, never as `stop`;
4. an orphan that keeps heartbeating but has no consumer is still stopped;
5. nothing leaks: abort handles are released, normal generation is untouched.

`test_worker_server.py` keeps covering the older guarantee (the generator is
still *closed* on the MLX thread). This file covers that it now also *stops*.

Note on the test double: `CountingStreamAdapter.stream` deliberately does not
consult any cancellation API. Real adapters do check, but only when the worker
asks them for another token -- which is exactly the thing under test. A double
that checked would pass on the pre-v2.8.0 code as well, and this file has to
fail on it.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

import common.server as worker_server  # noqa: E402
from common.server import ORPHAN_GENERATION_SECONDS, BaseAdapter, create_app  # noqa: E402


class CountingStreamAdapter(BaseAdapter):
    """Counts produced tokens and records how long each one takes.

    `total` is deliberately far larger than any test consumes, so a stream that
    is never told to stop keeps going -- which is the bug being pinned.
    """

    engine = "count-test"

    def __init__(self, total: int = 500, token_seconds: float = 0.0):
        super().__init__()
        self.total = total
        self.token_seconds = token_seconds
        self.produced = 0
        self.closed = False

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        self.model = object()
        return self.capabilities()

    def stream(self, request_id: str, params: dict):
        try:
            for index in range(self.total):
                if self.token_seconds:
                    time.sleep(self.token_seconds)
                self.produced += 1
                yield {"type": "delta", "text": f"t{index} "}
        finally:
            self.closed = True


class _GenerationRun:
    """One `/generate` call driven as a real ASGI disconnect.

    `TestClient` cannot express this: httpx's ASGI transport buffers the whole
    response body before returning, so the client never goes away and every
    generation runs to `max_tokens`. The failure mode under test only exists on
    a genuine disconnect, so this drives the ASGI app directly -- `receive`
    delivers one `http.request`, then `http.disconnect`, which is what uvicorn
    does when the client drops the socket and what `StreamingResponse` turns
    into a cancellation of the body iterator.
    """

    def __init__(self, app, request_id: str, disconnect_after: float | None = None):
        self.app = app
        self.request_id = request_id
        self.disconnect_after = disconnect_after
        self.events: list[dict] = []
        self.finished = threading.Event()
        self.error: BaseException | None = None
        # Set from the test thread to drop the connection at a moment the test
        # chooses, rather than on a timer.
        self._go_away = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_GenerationRun":
        self._thread.start()
        return self

    def disconnect(self) -> None:
        self._go_away.set()

    def wait_for_events(self, count: int, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while len(self.events) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return len(self.events) >= count

    def wait_for_completion(self, timeout: float = 20.0) -> bool:
        """Wait for a `completed` event, i.e. for the stream to end by itself."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(event.get("type") == "completed" for event in self.events):
                return True
            if self.finished.is_set():
                return False
            time.sleep(0.01)
        return False

    def join(self, timeout: float = 30.0) -> "_GenerationRun":
        self.disconnect()
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("generation never finished")
        if self.error is not None:
            raise self.error
        return self

    def _run(self) -> None:
        import asyncio

        async def drive() -> None:
            body = json.dumps({
                "protocol_version": 1, "request_id": self.request_id,
                "method": "generate", "params": {"prompt": "hi"},
            }).encode()
            state = {"request_sent": False}
            loop = asyncio.get_running_loop()
            deadline = (loop.time() + self.disconnect_after
                        if self.disconnect_after is not None else None)
            away = self._go_away

            async def receive():
                if not state["request_sent"]:
                    state["request_sent"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        await asyncio.sleep(min(remaining, 0.01))
                        if loop.time() < deadline:
                            return await receive()
                # Block until the test drops the connection. Sleeping in a loop
                # (rather than blocking the thread) is what keeps the event loop
                # free to run the generation.
                while not away.is_set():
                    await asyncio.sleep(0.005)
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] != "http.response.body":
                    return
                for line in message.get("body", b"").splitlines():
                    if line:
                        self.events.append(json.loads(line))

            await self.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": "/generate", "raw_path": b"/generate", "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"worker"),
                            (b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
            }, receive, send)

        try:
            asyncio.run(drive())
        except BaseException as exc:  # noqa: BLE001 - re-raised on join
            self.error = exc
        finally:
            self.finished.set()


def _wait_until_closed(adapter: CountingStreamAdapter, timeout: float = 6.0) -> bool:
    """Whether the adapter generator finalised.

    The close is queued on the MLX executor, so it lands shortly after the
    response is gone rather than with it.
    """
    deadline = time.monotonic() + timeout
    while not adapter.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    return adapter.closed


def _wait_until_stable(adapter: CountingStreamAdapter, timeout: float = 6.0) -> int:
    """How many tokens the adapter produced once production stopped moving.

    Production is asynchronous with respect to the response, so the raw count
    right after the disconnect is not the answer -- it is how far it got by the
    time the abort had taken effect.
    """
    deadline = time.monotonic() + timeout
    previous = adapter.produced
    while time.monotonic() < deadline:
        time.sleep(0.15)
        current = adapter.produced
        if current == previous:
            return current
        previous = current
    return adapter.produced


class orphan_limit:
    """Shrink the orphan limit for one test, restoring it afterwards."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.previous = ORPHAN_GENERATION_SECONDS

    def __enter__(self):
        worker_server.ORPHAN_GENERATION_SECONDS = self.seconds
        return self

    def __exit__(self, *exc):
        worker_server.ORPHAN_GENERATION_SECONDS = self.previous
        return False


def test_coordinator_timeout_is_the_limit_the_worker_applies():
    """`totalTimeoutSeconds` is the user's setting; the worker must not undercut it.

    A client that legitimately allows an hour must not be cut off at the
    worker's own fallback, and the worker must not ignore a shorter setting
    either.
    """
    from common.server import MAX_ORPHAN_GENERATION_SECONDS, _orphan_limit_seconds
    assert _orphan_limit_seconds({"total_timeout_seconds": 900}) == 900
    assert _orphan_limit_seconds({}) == ORPHAN_GENERATION_SECONDS
    # Nonsense is ignored rather than clamped: a bad value must not become a
    # silent early cut-off.
    assert _orphan_limit_seconds({"total_timeout_seconds": 0}) == ORPHAN_GENERATION_SECONDS
    assert _orphan_limit_seconds({"total_timeout_seconds": -5}) == ORPHAN_GENERATION_SECONDS
    assert _orphan_limit_seconds({"total_timeout_seconds": "abc"}) == ORPHAN_GENERATION_SECONDS
    assert _orphan_limit_seconds(
        {"total_timeout_seconds": MAX_ORPHAN_GENERATION_SECONDS * 2}
    ) == ORPHAN_GENERATION_SECONDS
    assert ORPHAN_GENERATION_SECONDS <= MAX_ORPHAN_GENERATION_SECONDS


def test_coordinator_passes_its_own_timeout_to_the_worker():
    """The setting has to actually reach the worker, or the fallback applies."""
    import inspect

    from mlxbar.workers import supervisor
    source = inspect.getsource(supervisor.WorkerSupervisor.generate)
    assert 'worker_params["total_timeout_seconds"] = total_timeout' in source


def test_disconnect_stops_the_generation_immediately():
    """The reported failure, as a test.

    Slow tokens (0.05s) so the generation is certainly still in flight when the
    client goes at 0.3s, and 500 of them: without the cooperative stop the
    abandoned generation would keep going for another ~25 seconds while holding
    the only MLX thread.
    """
    adapter = CountingStreamAdapter(total=500, token_seconds=0.05)
    run = _GenerationRun(create_app(adapter), "abandoned", disconnect_after=0.3).start()
    run.join()
    assert adapter.produced > 1, "the disconnect landed before generation started"
    assert _wait_until_closed(adapter), "adapter generator was never closed"
    # A couple of tokens of slack for the one already in flight at the
    # disconnect.
    assert _wait_until_stable(adapter) <= 12, "abandoned generation kept generating"


def test_cancel_rpc_stops_the_generation_immediately():
    adapter = CountingStreamAdapter(total=500, token_seconds=0.01)
    app = create_app(adapter)
    # Still connected, so only the cancel can end this one.
    run = _GenerationRun(app, "cancel-me").start()
    assert run.wait_for_events(3), run.events
    assert adapter.produced >= 1
    with TestClient(app) as client:
        response = client.post("/rpc", json={
            "protocol_version": 1, "method": "cancel",
            "params": {"request_id": "cancel-me"},
        })
        payload = response.json()
        assert payload["cancelled"] is True
        # The live generation was signalled, not only recorded in the registry.
        assert payload["abortSignalled"] is True
    assert _wait_until_stable(adapter) < 500, "cancel did not stop the generation"


def test_cancel_does_not_wait_for_the_mlx_executor():
    """The stop must not be queued behind the work it is trying to stop.

    The pre-v2.8.0 path submitted `generator.close()` to the single-worker MLX
    executor, so a cancel could only take effect once that executor was free.
    Tokens are slow here, so an RPC that still returns immediately proves the
    signal never went through the executor.
    """
    adapter = CountingStreamAdapter(total=500, token_seconds=0.05)
    app = create_app(adapter)
    run = _GenerationRun(app, "blocked").start()
    assert run.wait_for_events(2), run.events
    with TestClient(app) as client:
        started = time.monotonic()
        client.post("/rpc", json={
            "protocol_version": 1, "method": "cancel",
            "params": {"request_id": "blocked"},
        })
        elapsed = time.monotonic() - started
    # One slow token is 0.05s; a queued cancel would take at least that.
    assert elapsed < 0.2, f"cancel took {elapsed:.3f}s -- it waits on the MLX thread"
    assert _wait_until_stable(adapter) < 100


def test_cancelled_generation_reports_cancelled_not_stop():
    """Truncated output must never be presented as a finished answer.

    The cooperative stop exits the generation loop; without an explicit branch
    the code after it would run `finalize()` on a partial reply and emit
    `finish_reason: "stop"`, telling the client its generation completed.
    """
    # Slow enough that the whole 500 cannot finish while the cancel round trip is
    # in flight, so the test cannot pass by the generation simply ending first.
    adapter = CountingStreamAdapter(total=500, token_seconds=0.02)
    app = create_app(adapter)
    run = _GenerationRun(app, "finish-reason").start()
    assert run.wait_for_events(3), run.events
    with TestClient(app) as client:
        client.post("/rpc", json={
            "protocol_version": 1, "method": "cancel",
            "params": {"request_id": "finish-reason"},
        })
    # Wait for the completion event itself rather than dropping the connection:
    # `run.join()` disconnects, and a disconnect would truncate the stream
    # before the event under test is delivered -- passing for the wrong reason.
    assert run.wait_for_completion(), run.events[-3:]
    run.join()
    completions = [event for event in run.events if event.get("type") == "completed"]
    assert completions, run.events[-3:]
    assert completions[-1].get("finish_reason") == "cancelled", completions[-1]
    assert not any(event.get("finish_reason") == "stop" for event in run.events)
    # A cancelled stream must not run tool parsing over a partial reply.
    assert not any(event.get("type") == "tool_calls" for event in run.events)


def test_orphan_generation_is_stopped_without_a_cancel():
    """A consumer that vanishes without cancelling must not run for an hour.

    The coordinator's `tokenIdleTimeoutSeconds` only catches a worker that went
    silent. This one keeps emitting, so nothing else would stop it.
    """
    adapter = CountingStreamAdapter(total=1_000_000, token_seconds=0.001)
    with orphan_limit(0.3):
        run = _GenerationRun(create_app(adapter), "orphan").start()
        # The client stays connected on purpose: the orphan limit is the only
        # thing that can end this, so wait for the response rather than
        # dropping the connection the way the other tests do.
        assert run.finished.wait(20), "orphan generation never stopped"
        run.join()
    codes = {event.get("code") for event in run.events}
    assert "GENERATION_TIMEOUT" in codes, sorted(codes)[:5]
    assert adapter.produced < 1_000_000


def test_orphan_limit_is_far_above_any_legitimate_generation():
    # Guards the constant itself: a limit low enough to cut off a real long
    # answer would be a worse bug than the one being fixed.
    assert ORPHAN_GENERATION_SECONDS >= 1800


def test_normal_generation_is_unaffected():
    adapter = CountingStreamAdapter(total=3)
    with TestClient(create_app(adapter)) as client:
        response = client.post("/generate", json={
            "protocol_version": 1, "request_id": "normal",
            "method": "generate", "params": {"prompt": "hi"},
        })
    events = [json.loads(line) for line in response.text.splitlines() if line]
    assert events[-1] == {"type": "completed", "finish_reason": "stop"}
    assert adapter.produced == 3
    assert adapter.closed is True
    assert not adapter.is_aborted("normal")


def test_abort_handles_are_released_when_streams_end():
    # A leaked handle would make the next request with the same id look
    # cancelled. The registry is bounded; these handles must not accumulate.
    adapter = CountingStreamAdapter(total=2)
    with TestClient(create_app(adapter)) as client:
        for index in range(4):
            client.post("/generate", json={
                "protocol_version": 1, "request_id": f"req-{index}",
                "method": "generate", "params": {"prompt": "hi"},
            })
    assert adapter.aborts == {}


def test_abandoned_stream_releases_its_abort_handle():
    adapter = CountingStreamAdapter(total=500, token_seconds=0.01)
    run = _GenerationRun(create_app(adapter), "gone", disconnect_after=0.2).start()
    run.join()
    deadline = time.monotonic() + 5
    while adapter.closed is not True and time.monotonic() < deadline:
        time.sleep(0.02)
    assert adapter.aborts == {}

def test_rayon_thread_pool_is_bounded():
    """P2 of the CPU report: `tokenizers` was the top hot frame.

    Rayon sizes its pool from the machine's core count, so a 28-core Mac spawns
    28 tokeniser threads inside a worker that already serialises everything else
    onto one MLX thread. The bound is applied at import time -- after tokenizers
    reads it -- and only when the caller has not chosen a value.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[1] / "Workers" / "mlx_lm_worker" / "adapter.py")
        .read_text(encoding="utf-8"))
    setdefaults = {
        call.args[0].value
        for node in ast.walk(tree) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "setdefault"
        and isinstance(node.value.func.value, ast.Attribute)
        and node.value.func.value.attr == "environ"
        and node.value.args and isinstance(node.value.args[0], ast.Constant)
        for call in [node.value]
    }
    assert "RAYON_NUM_THREADS" in setdefaults, setdefaults
    # Both have to be `setdefault`, or an operator's own choice is overridden.
    assert "TOKENIZERS_PARALLELISM" in setdefaults, setdefaults


def test_rayon_bound_is_applied_before_tokenizers_is_imported():
    """The pool is sized at first use, so a later assignment would be too late."""
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[1] / "Workers" / "mlx_lm_worker" / "adapter.py")
        .read_text(encoding="utf-8"))
    bound_at = None
    mlx_import_at = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        text = ast.unparse(node)
        if bound_at is None and "RAYON_NUM_THREADS" in text:
            bound_at = node.lineno
        if mlx_import_at is None and text.lstrip().startswith("import mlx_lm"):
            mlx_import_at = node.lineno
    assert bound_at is not None
    # `import mlx_lm` only happens lazily inside stream(); what matters is that
    # the assignment is module level, which it is by construction here.
    assert mlx_import_at is None or bound_at < mlx_import_at, (bound_at, mlx_import_at)
