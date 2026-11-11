"""Continuous batching for one worker process (v5.1.0).

One loaded model, one MLX thread, several sequences: instead of serialising
requests (or loading a second copy of the weights), the worker hands every
request to a single scheduler that drives the runtime's own batched generator
(``mlx_lm.generate.BatchGenerator``) and routes each token back to the request
that produced it.

The design rules this module exists to enforce:

* **One GPU thread.** Every MLX call happens on the thread that created the
  scheduler, because MLX keeps stream state per thread. The scheduler runs in a
  dedicated thread and publishes plain dicts; nothing here touches MLX from an
  asyncio thread.
* **Per-request isolation.** Each request keeps its own sampler, logits
  processors, stop sequences, token budget and KV cache (the runtime allocates
  one cache per inserted sequence). A failure in one request removes only that
  request.
* **No silent regeneration.** If the batched runtime raises, every request in
  the batch receives an error event. Nothing is retried behind the client's
  back: a partial answer is never restarted, which is the contract the
  single-generation path already keeps.
* **Bounded memory.** The scheduler only runs while it has work, and it drops
  each finished request's cache (``BatchGenerator.remove``) before the next
  step, so a finished sequence never holds KV memory for the next one.

What is deliberately *not* here (v5.1.0): prompt-cache reuse, KV quantization,
paged KV, adaptive memory and speculative decoding. Those features change the
cache the runtime is asked to batch, and the mlx-lm server itself refuses to
batch when any of them is active. The coordinator falls back to the ordinary
single-generation path for such requests (see ``mlx_lm_worker.adapter``).

The runtime API used here is checked with ``hasattr`` rather than by version
number, and a missing capability simply reports ``capable: False`` -- the same
rule the rest of the worker follows for runtime internals.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

#: How long a request waits for its first event before the HTTP layer's own
#: heartbeat takes over. Only used to bound ``queue.get`` so the scheduler can
#: notice a shutdown; the value itself is never reported to a client.
POLL_SECONDS = 0.05
#: Upper bound on a request's output queue. A client that stops reading must
#: not make the scheduler allocate without limit; the generation is cancelled
#: instead, which is the same outcome as a disconnect.
MAX_QUEUED_EVENTS = 4096


class BatchUnavailable(RuntimeError):
    """The runtime cannot batch this model. Callers fall back to serial."""


@dataclass
class BatchRequest:
    """One sequence submitted to the scheduler."""

    request_id: str
    prompt_tokens: list[int]
    max_tokens: int
    sampler: object | None = None
    logits_processors: list | None = None
    stop_sequences: object | None = None
    #: Detokenizer state is owned by the caller (the adapter), because the
    #: tokenizer lives there. The scheduler only reports token ids.
    events: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    uid: int | None = None
    cancelled: bool = False
    finished: bool = False
    generated: list[int] = field(default_factory=list)
    prompt_tokens_processed: int = 0
    started_at: float = 0.0
    first_token_at: float | None = None
    finished_at: float | None = None
    finish_reason: str | None = None


def _capability_probe(make_prompt_cache, model) -> tuple[bool, str | None]:
    """Whether the runtime can batch *this* model, and why not.

    Mirrors ``mlx_lm.server.ModelProvider._is_batchable``: every cache layer the
    model creates must implement ``merge`` (the batch generator merges the
    per-sequence caches into one batched cache each step). A hybrid or
    recurrent cache without ``merge`` cannot be batched, and pretending
    otherwise would produce wrong output rather than an error.
    """
    try:
        caches = make_prompt_cache(model)
    except Exception as exc:  # noqa: BLE001 - a probe must never fail a load
        return False, f"prompt_cache_unavailable:{type(exc).__name__}"
    try:
        layers = list(caches)
    except TypeError:
        return False, "prompt_cache_not_list"
    if not layers:
        return False, "prompt_cache_empty"
    missing = [type(layer).__name__ for layer in layers
               if not hasattr(layer, "merge")]
    if missing:
        return False, f"cache_without_merge:{missing[0]}"
    return True, None


class BatchScheduler:
    """Drives ``BatchGenerator`` on one thread for one loaded model.

    Lifecycle: ``start()`` once after the model is loaded, ``submit()`` per
    request, ``cancel()`` per request, ``shutdown()`` before the model is
    released. Every method is safe to call from the event loop thread; only
    the scheduler thread touches MLX.
    """

    def __init__(self, model, *, max_sequences: int = 2, prefill_batch_size: int = 2,
                 prefill_step_size: int = 2048, make_prompt_cache=None,
                 batch_generator_factory=None, tokenizer=None):
        self.model = model
        self.max_sequences = max(1, int(max_sequences))
        self.prefill_batch_size = max(1, min(self.max_sequences, int(prefill_batch_size)))
        self.prefill_step_size = max(256, int(prefill_step_size))
        self.tokenizer = tokenizer
        self._make_prompt_cache = make_prompt_cache
        self._factory = batch_generator_factory
        self._generator = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._inbox: "queue.Queue[BatchRequest | None]" = queue.Queue()
        self._pending: list[BatchRequest] = []
        self._active: dict[int, BatchRequest] = {}
        self._stopping = False
        self._idle = threading.Event()
        self._idle.set()
        self.capable = False
        self.capable_reason: str | None = None
        self.steps = 0
        self.batch_peak = 0
        self.errors = 0

    # --- capability -------------------------------------------------------

    def probe(self) -> bool:
        """Decide whether this model can be batched, without starting the thread."""
        if self._make_prompt_cache is None or self._factory is None:
            self.capable, self.capable_reason = False, "runtime_without_batch_generator"
            return False
        ok, reason = _capability_probe(self._make_prompt_cache, self.model)
        self.capable, self.capable_reason = ok, reason
        return ok

    def status(self) -> dict:
        with self._lock:
            active = len(self._active)
            queued = len(self._pending)
        return {
            "capable": bool(self.capable),
            "reason": self.capable_reason,
            "maxSequences": self.max_sequences,
            "activeSequences": active,
            "queuedSequences": queued,
            "steps": self.steps,
            "peakSequences": self.batch_peak,
            "errors": self.errors,
        }

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self.capable:
            raise BatchUnavailable(self.capable_reason or "not capable")
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="mlxbar-batch",
                                        daemon=True)
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stopping = True
        self._inbox.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        generator, self._generator = self._generator, None
        if generator is not None:
            try:
                generator.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                LOGGER.debug("BatchGenerator.close() failed during shutdown", exc_info=True)

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        return self._idle.wait(timeout)

    # --- request API ------------------------------------------------------

    def submit(self, request: BatchRequest) -> None:
        if not self.capable:
            raise BatchUnavailable(self.capable_reason or "not capable")
        request.started_at = time.monotonic()
        self._idle.clear()
        self._inbox.put(request)

    def cancel(self, request_id: str) -> bool:
        """Mark one request cancelled. The scheduler removes it at the next step."""
        with self._lock:
            for request in self._pending:
                if request.request_id == request_id:
                    request.cancelled = True
                    return True
            for request in self._active.values():
                if request.request_id == request_id:
                    request.cancelled = True
                    return True
        return False

    # --- scheduler thread -------------------------------------------------

    def _run(self) -> None:
        try:
            self._generator = self._factory(
                self.model,
                completion_batch_size=self.max_sequences,
                prefill_batch_size=self.prefill_batch_size,
                prefill_step_size=self.prefill_step_size,
            )
        except Exception as exc:  # noqa: BLE001 - reported to every waiter
            self.capable = False
            self.capable_reason = f"batch_generator_init_failed:{type(exc).__name__}"
            LOGGER.exception("BatchGenerator could not be created")
            self._fail_all({"type": "error", "code": "GENERATION_FAILED",
                            "message": "連続バッチ処理を開始できませんでした",
                            "detail": str(exc)[-1000:], "retryable": False})
            self._idle.set()
            return
        while not self._stopping:
            self._drain_inbox()
            self._insert_pending()
            self._apply_cancellations()
            if not self._active:
                self._idle.set()
                try:
                    item = self._inbox.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                if item is None:
                    break
                # Keep the request for the next iteration's `_drain_inbox`
                # instead of pushing it back: the loop's next step drains the
                # inbox, and re-queueing an item that is still held here would
                # spin forever without ever inserting it.
                self._pending.append(item)
                continue
            self._idle.clear()
            try:
                prompt_responses, generation_responses = self._generator.next()
            except Exception as exc:  # noqa: BLE001 - whole batch fails together
                self.errors += 1
                LOGGER.exception("Batched generation failed")
                self._fail_all({"type": "error", "code": "GENERATION_FAILED",
                                "message": "生成に失敗しました（ランタイムエラー）",
                                "detail": str(exc)[-1000:], "retryable": False})
                self._reset_generator()
                continue
            self.steps += 1
            self._emit_prompt(prompt_responses)
            self._emit_generation(generation_responses)
        self._fail_all({"type": "error", "code": "GENERATION_FAILED",
                        "message": "ワーカーが停止しました", "retryable": True})
        self._idle.set()

    def _drain_inbox(self) -> None:
        while True:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                return
            if item is None:
                self._stopping = True
                return
            self._pending.append(item)

    def _insert_pending(self) -> None:
        # `_pending` is the queue of requests not inserted yet, so the room is
        # simply what is left of the batch: subtracting `_pending` as well
        # would count them twice and refuse every request when two arrive at
        # once.
        room = self.max_sequences - len(self._active)
        while self._pending and room > 0:
            request = self._pending.pop(0)
            if request.cancelled:
                self._finish(request, "cancelled", emit_completed=True)
                continue
            try:
                (uid,) = self._generator.insert(
                    [request.prompt_tokens],
                    max_tokens=[request.max_tokens],
                    samplers=[request.sampler] if request.sampler is not None else None,
                    logits_processors=([request.logits_processors]
                                       if request.logits_processors is not None else None),
                    stop_sequences=([request.stop_sequences]
                                    if request.stop_sequences is not None else None),
                )
            except Exception as exc:  # noqa: BLE001 - one bad request, not the batch
                LOGGER.warning("Could not insert a batched request: %s", exc)
                self._put(request, {"type": "error", "code": "GENERATION_FAILED",
                                    "message": "生成を開始できませんでした",
                                    "detail": str(exc)[-1000:], "retryable": False})
                self._finish(request, "error", emit_completed=False)
                continue
            request.uid = uid
            with self._lock:
                self._active[uid] = request
            self.batch_peak = max(self.batch_peak, len(self._active))
            room -= 1

    def _apply_cancellations(self) -> None:
        with self._lock:
            cancelled = [uid for uid, request in self._active.items() if request.cancelled]
        if not cancelled:
            return
        try:
            self._generator.remove(cancelled)
        except Exception:  # noqa: BLE001 - removal is best effort
            LOGGER.debug("BatchGenerator.remove() failed", exc_info=True)
        for uid in cancelled:
            with self._lock:
                request = self._active.pop(uid, None)
            if request is not None:
                self._finish(request, "cancelled", emit_completed=True)

    def _emit_prompt(self, responses) -> None:
        for response in responses or []:
            with self._lock:
                request = self._active.get(getattr(response, "uid", None))
            if request is None:
                continue
            progress = getattr(response, "progress", None)
            if progress:
                request.prompt_tokens_processed = int(progress[0] or 0)
            self._put(request, {"type": "prompt_progress",
                                "processed_tokens": request.prompt_tokens_processed,
                                "total_tokens": int((progress[1] if progress else 0) or 0)})

    def _emit_generation(self, responses) -> None:
        for response in responses or []:
            uid = getattr(response, "uid", None)
            with self._lock:
                request = self._active.get(uid)
            if request is None:
                continue
            token = getattr(response, "token", None)
            finish_reason = getattr(response, "finish_reason", None)
            is_token = isinstance(token, int) and not isinstance(token, bool)
            # The token that carried a stop signal is *not* part of the answer:
            # it is the first token of the matched stop sequence, and the
            # runtime reports no text for it. Publishing it would leak the
            # stop text to the client, so a finishing response publishes only
            # its reason. `length` is different: its last token is real output.
            publish = is_token and (finish_reason is None or finish_reason == "length")
            if publish:
                request.generated.append(token)
                if request.first_token_at is None:
                    request.first_token_at = time.monotonic()
                self._put(request, {"type": "token", "token": token})
            if finish_reason is not None:
                with self._lock:
                    self._active.pop(uid, None)
                self._put(request, {"type": "finished", "finish_reason": finish_reason,
                                    "generated_tokens": len(request.generated)})
                self._finish(request, finish_reason, emit_completed=False)

    def _finish(self, request: BatchRequest, reason: str, *, emit_completed: bool) -> None:
        request.finished = True
        request.finish_reason = reason
        request.finished_at = time.monotonic()
        if emit_completed:
            self._put(request, {"type": "finished", "finish_reason": reason,
                                "generated_tokens": len(request.generated)})

    def _put(self, request: BatchRequest, event: dict) -> None:
        if request.events.qsize() >= MAX_QUEUED_EVENTS:
            # A client that stopped draining its stream: cancel rather than
            # grow without bound. The adapter turns this into a cancelled end.
            request.cancelled = True
            return
        request.events.put(event)

    def _fail_all(self, event: dict) -> None:
        with self._lock:
            requests = list(self._active.values())
            self._active.clear()
            requests.extend(self._pending)
            self._pending.clear()
        for request in requests:
            self._put(request, dict(event))
            request.finished = True
            request.finish_reason = "error"
            request.finished_at = time.monotonic()

    def _reset_generator(self) -> None:
        generator, self._generator = self._generator, None
        if generator is not None:
            try:
                generator.close()
            except Exception:  # noqa: BLE001
                LOGGER.debug("BatchGenerator.close() failed", exc_info=True)
        try:
            self._generator = self._factory(
                self.model,
                completion_batch_size=self.max_sequences,
                prefill_batch_size=self.prefill_batch_size,
                prefill_step_size=self.prefill_step_size,
            )
        except Exception:  # noqa: BLE001
            self.capable = False
            self.capable_reason = "batch_generator_reinit_failed"
            LOGGER.exception("BatchGenerator could not be recreated")
            self._stopping = True
