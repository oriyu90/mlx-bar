from __future__ import annotations

import contextlib
import gc
import importlib.metadata
import logging
import os
import queue
import time

# v2.8.0: bound the Rust tokeniser's thread pool (P2 of the CPU report, where
# `tokenizers` was the single hottest frame). v2.8.1 moved the actual
# `setdefault` calls into `common/server.py` so the mlx-vlm worker inherits
# the same bounds; the checks below pin the expected values for this adapter's
# test harness (TEST_PLAN_v2.8.0.md / TEST_PLAN_v2.8.1.md).
os.environ.setdefault("RAYON_NUM_THREADS", str(min(4, os.cpu_count() or 4)))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

LOGGER = logging.getLogger(__name__)

from common import cache_state
from common import vision
from common.batch import BatchRequest, BatchScheduler, BatchUnavailable
from common.jang import raise_if_jang
from common.model_errors import ModelLoadError
from common.server import BaseAdapter, run
from common.tool_calls import normalize_tool_call_messages, parse_tool_markup, tool_template_kwargs_attempts

from .prompt_cache import PromptCacheStore


class MLXLMAdapter(BaseAdapter):
    engine = "mlx-lm"

    def __init__(self):
        super().__init__()
        self.prompt_cache: PromptCacheStore | None = None
        self.model_path: str | None = None
        self.cache_budget: dict = {"known": False}
        self.last_cold_reason: str | None = None
        self._pending_cold_reason: str | None = None
        self._cold_prompt_tps = 0.0
        # Live KV quantization (v2.5.0). None means full-precision bf16,
        # matching the pre-v2.5.0 behaviour byte-for-byte.
        self.kv_bits: int | None = None
        self.kv_group_size = 64
        self.kv_start = 0
        self.kv_mode = "bf16"
        self.kv_fallback_reason: str | None = None
        # v5.1.0 continuous batching. None until a load decides; the scheduler
        # itself starts lazily on the first batched request.
        self.batch_scheduler: BatchScheduler | None = None
        self._batch_capable = False
        self._batch_reason: str | None = "not_loaded"
        self._batch_settings_latched: dict = {}
        self._batch_requests: dict[str, BatchRequest] = {}

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["promptCaching"] = self.prompt_cache is not None
        result["promptCache"] = self.prompt_cache_stats()
        result["cacheBudget"] = dict(self.cache_budget)
        result["rollbackCapability"] = cache_state.ROLLBACK_TRIM
        result["kvQuant"] = self._kv_quant_status()
        # v5.1.0: the coordinator decides whether to route a second request to
        # this worker from this flag, so it must reflect the loaded model, not
        # the settings.
        result["continuousBatching"] = self.batch_status()
        return result

    def _kv_quant_status(self) -> dict:
        return {
            "mode": self.kv_mode,
            "bits": self.kv_bits,
            "groupSize": self.kv_group_size,
            "quantizedStart": self.kv_start,
            "active": self.kv_bits is not None,
            "fallbackReason": self.kv_fallback_reason,
        }

    @staticmethod
    def _truthy(value: str | None) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _init_prompt_cache(self, path: str) -> None:
        try:
            runtime_version = importlib.metadata.version("mlx-lm")
        except Exception:
            runtime_version = "unknown"
        try:
            max_bytes = int(os.environ.get("MLXBAR_PROMPT_CACHE_MAX_BYTES", str(10 << 30)))
        except (TypeError, ValueError):
            max_bytes = 10 << 30
        try:
            keep = int(os.environ.get("MLXBAR_PROMPT_CACHE_KEEP_GENERATIONS", "2"))
        except (TypeError, ValueError):
            keep = 2
        try:
            paged_max_bytes = int(os.environ.get("MLXBAR_PAGED_KV_MAX_BYTES", "0"))
        except (TypeError, ValueError):
            paged_max_bytes = 0
        self._init_kv_quant_from_env()
        self.prompt_cache = PromptCacheStore(
            path, runtime_version,
            root=os.environ.get("MLXBAR_PROMPT_CACHE_ROOT"),
            disk_enabled=self._truthy(os.environ.get("MLXBAR_PROMPT_CACHE_DISK_ENABLED", "1")),
            max_bytes=max_bytes,
            keep_generations=min(10, max(1, keep)),
            model=self.model,
            cache_budget=self.cache_budget,
            paged_enabled=self._truthy(os.environ.get("MLXBAR_PAGED_KV_ENABLED")),
            paged_root=os.environ.get("MLXBAR_PAGED_KV_ROOT"),
            paged_disk_enabled=self._truthy(os.environ.get("MLXBAR_PAGED_KV_DISK_ENABLED", "1")),
            paged_max_bytes=max(0, paged_max_bytes),
            paged_branch_reuse=self._truthy(os.environ.get("MLXBAR_PAGED_KV_BRANCH_REUSE", "1")),
            kv_mode=self.kv_mode,
            kv_bits=self.kv_bits,
        )

    def _init_kv_quant_from_env(self) -> None:
        """Read the coordinator-latched quantization mode without failing load.

        Unknown or malformed values fall back to full-precision bf16: losing
        a memory saving must never keep a model from loading.
        """
        raw_bits = (os.environ.get("MLXBAR_KV_QUANT_BITS") or "").strip()
        raw_mode = (os.environ.get("MLXBAR_KV_QUANT_MODE") or "").strip()
        try:
            group_size = int(os.environ.get("MLXBAR_KV_QUANT_GROUP_SIZE", "64"))
        except (TypeError, ValueError):
            group_size = 64
        try:
            start = int(os.environ.get("MLXBAR_KV_QUANT_START", "0"))
        except (TypeError, ValueError):
            start = 0
        if group_size not in {32, 64, 128}:
            group_size = 64
        if start < 0:
            start = 0
        self.kv_group_size = group_size
        self.kv_start = start
        self.kv_fallback_reason = None
        if raw_bits == "8" or raw_mode == "8bit":
            self.kv_bits, self.kv_mode = 8, "8bit"
        elif raw_bits == "4" or raw_mode == "4bit":
            self.kv_bits, self.kv_mode = 4, "4bit"
        else:
            self.kv_bits, self.kv_mode = None, "bf16"

    def _kv_generate_kwargs(self) -> dict:
        if self.kv_bits is None:
            return {}
        return {"kv_bits": self.kv_bits, "kv_group_size": self.kv_group_size,
                "quantized_kv_start": self.kv_start}

    def load(self, path: str, trust_remote_code: bool = False, allow_vlm: bool = False) -> dict:
        if not path:
            raise ValueError("model path is required")
        # v5.0.1: JANG (jangtq2) artifacts cannot be served by stock mlx-lm
        # either. The mlx-vlm worker rejects them in preflight
        # (mlx_vlm_worker/loaders.generic_preflight); this worker has no
        # loaders.plan() call of its own, so the identical check runs here
        # via the shared common/jang.py rule -- same evidence, same
        # MODEL_JANG_OSAURUS_REQUIRED code, before stock `load()` can die with
        # a rotation error far from the cause. Reads only config.json /
        # jang_config.json (+ osaurus.json existence), never safetensors.
        try:
            import json as _json
            from pathlib import Path as _Path

            try:
                _config = _json.loads(
                    (_Path(path) / "config.json").read_text(encoding="utf-8"))
            except Exception:
                _config = None
            raise_if_jang(_config if isinstance(_config, dict) else None, path)
        except ModelLoadError:
            raise
        except Exception:  # noqa: BLE001
            # Evidence-read failures fall through to stock on purpose so a
            # normal model's transient config-read problem cannot block its
            # load; a JANG artifact still fails later at the stock loader's
            # quantization rotation error, so no wrong output is produced.
            pass
        # v2.10.0 vision guard: a model the coordinator (or the operator)
        # did NOT explicitly pin to mlx-lm must never be text-served here if
        # it is a vision-language model — its image inputs would be dropped
        # end to end. Raise a tagged error the server classifies so the
        # coordinator Candidate chain can retry with mlx-vlm. An explicit
        # user engine choice passes `allow_vlm=True` and keeps working.
        if not allow_vlm and vision.looks_like_vlm(path):
            # v3.2.0 (WP-211): name the evidence, not just the verdict. The
            # coordinator aggregates this text into `attempts[]`, so a vague
            # message ("load it with mlx-vlm") used to be the only thing the
            # user saw even when mlx-vlm was the engine that actually failed.
            reading = vision.inspect(path)
            keys = ", ".join(reading.get("visionConfigKeys") or []) or "なし"
            tensors = reading.get("tensors") or {}
            tensor_text = (f"visionテンソル {tensors.get('vision')}/{tensors.get('total')}件"
                           if tensors.get("known") else "テンソル情報なし")
            raise RuntimeError(
                f"{vision.GUARD_TAG}: vision-language model detected "
                f"(vision設定キー: {keys}; {tensor_text}). "
                "mlx-lm cannot serve image inputs for this model — loading it here "
                "would silently drop every image. Use the mlx-vlm engine, or pass "
                "allow_vlm=true to force an explicit text-only selection.")

        # The allocator ceiling has to exist before the runtime starts creating
        # arrays. Applying it only after load cannot contain a cold-load peak.
        self.apply_memory_limits()
        from mlx_lm import load
        tried = False
        for allow_remote in (trust_remote_code, True):
            if tried and not allow_remote:
                continue
            tried = True
            try:
                self.model, self.processor = load(path, tokenizer_config={"trust_remote_code": allow_remote})
                break
            except TypeError:
                self.model, self.processor = load(path)
                break
            except Exception as exc:
                if allow_remote or "remote code" not in str(exc).lower():
                    raise
                # v3.2.0 (ISS-5): a failed load can leave a partially built
                # model (and its MLX buffers) behind. Drop it before retrying:
                # on a 100B-class checkpoint that residue is what turns one
                # failed attempt into an out-of-memory kill.
                self._release_partial_load()
                # retry with remote code enabled
        self.model_path = path
        self.apply_memory_limits()
        self.cache_budget = _read_cache_budget(path)
        self._init_prompt_cache(path)
        # v5.1.0: decide batchability once, from the loaded model's own caches
        # (never from a version number). Failure to probe is a fallback, not a
        # load error.
        try:
            self._init_batch_scheduler()
        except Exception:  # noqa: BLE001 - batching is an optimisation
            LOGGER.exception("Continuous batching probe failed; using single generation")
            self.batch_scheduler = None
            self._batch_capable = False
            self._batch_reason = "probe_failed"
        return self.capabilities()

    def _release_partial_load(self) -> None:
        """Drop a half-built model and its MLX buffers before a retry (ISS-5).

        The `trust_remote_code` retry runs the loader twice. Without this the
        first attempt's partially constructed model stays reachable until the
        next GC cycle, so a large checkpoint can exhaust memory during the
        retry instead of recovering.
        """
        self.model = None
        self.processor = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:  # noqa: BLE001 - cleanup must never mask the real error
            pass

    def unload(self) -> None:
        # v5.1.0: the batch scheduler owns MLX state (its own thread and a
        # BatchGenerator holding per-sequence caches). It must be stopped
        # before the weights are dropped, or the thread would keep a reference
        # to a model the coordinator believes is gone.
        self._stop_batch_scheduler()
        self.prompt_cache = None
        self.model_path = None
        super().unload()

    # --- v5.1.0 continuous batching --------------------------------------

    def _batch_settings(self) -> dict:
        """Batching limits from the coordinator's environment, fail-safe."""
        def _int(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        return {
            "enabled": self._truthy(os.environ.get("MLXBAR_BATCH_ENABLED", "0")),
            "maxSequences": _int("MLXBAR_BATCH_MAX_SEQUENCES", 2, 1, 8),
            "prefillBatchSize": _int("MLXBAR_BATCH_PREFILL_BATCH_SIZE", 2, 1, 8),
            "prefillStepSize": _int("MLXBAR_BATCH_PREFILL_STEP_SIZE", 2048, 256, 32768),
        }

    def _init_batch_scheduler(self) -> None:
        """Prepare (but do not start) the batch scheduler after a load.

        The scheduler is started lazily on the first batched request, so a
        model nobody batches never pays for the extra thread or the
        ``BatchGenerator``'s wired-limit bookkeeping.
        """
        self._stop_batch_scheduler()
        settings = self._batch_settings()
        self._batch_settings_latched = settings
        if not settings["enabled"] or self.model is None:
            self.batch_scheduler = None
            self._batch_capable, self._batch_reason = False, (
                "disabled" if not settings["enabled"] else "no_model")
            return
        try:
            from mlx_lm.generate import BatchGenerator
            from mlx_lm.models.cache import make_prompt_cache
        except Exception as exc:  # noqa: BLE001 - an older runtime is not an error
            self.batch_scheduler = None
            self._batch_capable = False
            self._batch_reason = f"runtime_without_batch_generator:{type(exc).__name__}"
            return
        scheduler = BatchScheduler(
            self.model,
            max_sequences=settings["maxSequences"],
            prefill_batch_size=settings["prefillBatchSize"],
            prefill_step_size=settings["prefillStepSize"],
            make_prompt_cache=make_prompt_cache,
            batch_generator_factory=BatchGenerator,
            tokenizer=self.processor,
        )
        if scheduler.probe():
            self.batch_scheduler = scheduler
            self._batch_capable = True
            self._batch_reason = None
        else:
            # Not an error: hybrid/recurrent caches without `merge` cannot be
            # batched, and the request falls back to single generation.
            self.batch_scheduler = None
            self._batch_capable = False
            self._batch_reason = scheduler.capable_reason

    def _stop_batch_scheduler(self) -> None:
        scheduler = getattr(self, "batch_scheduler", None)
        self.batch_scheduler = None
        self._batch_capable = False
        self._batch_reason = getattr(self, "_batch_reason", None) or "not_loaded"
        if scheduler is not None:
            try:
                scheduler.shutdown()
            except Exception:  # noqa: BLE001 - teardown must not raise
                LOGGER.debug("Batch scheduler shutdown failed", exc_info=True)

    def batch_status(self) -> dict:
        scheduler = getattr(self, "batch_scheduler", None)
        settings = getattr(self, "_batch_settings_latched", self._batch_settings())
        status = scheduler.status() if scheduler is not None else {}
        return {
            "capable": bool(getattr(self, "_batch_capable", False)),
            "reason": getattr(self, "_batch_reason", None),
            "maxSequences": settings.get("maxSequences", 1),
            "prefillBatchSize": settings.get("prefillBatchSize", 1),
            "prefillStepSize": settings.get("prefillStepSize", 2048),
            "started": scheduler is not None,
            "activeSequences": status.get("activeSequences", 0),
            "queuedSequences": status.get("queuedSequences", 0),
            "steps": status.get("steps", 0),
            "peakSequences": status.get("peakSequences", 0),
            "errors": status.get("errors", 0),
        }

    def _batch_eligible(self, params: dict) -> tuple[bool, str | None]:
        """Whether this request may run on the batched path, and why not.

        The conditions mirror ``mlx_lm.server.ModelProvider._is_batchable``
        plus this project's own cache features. Anything that changes the KV
        cache the runtime is asked to batch is refused rather than guessed at:
        a wrong answer here is silent corruption, while falling back is only
        slower.
        """
        scheduler = getattr(self, "batch_scheduler", None)
        if scheduler is None or not getattr(self, "_batch_capable", False):
            return False, getattr(self, "_batch_reason", None) or "not_capable"
        if params.get("images"):
            return False, "images"
        seed = params.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            # mlx-lm refuses to batch seeded requests: the sampler's RNG is
            # global, so a batched seed would not reproduce single generation.
            return False, "seed"
        if self.kv_bits is not None:
            return False, "kv_quantization"
        if self.prompt_cache is not None:
            # v5.1.0 keeps prompt-cache reuse on the single path. Sharing a
            # stored prefix between batched sequences is a per-sequence cache
            # question the runtime does not answer yet.
            return False, "prompt_cache"
        adaptive = params.get("adaptiveMemory") or {}
        if isinstance(adaptive, dict) and adaptive.get("enabled"):
            return False, "adaptive_memory"
        return True, None

    def _batch_sampler(self, params: dict):
        from mlx_lm.sample_utils import make_logits_processors, make_sampler

        temperature = float(params.get("temperature", 0.7))
        top_p = float(params.get("top_p", 1.0))
        sampler = make_sampler(temp=temperature, top_p=top_p)
        repetition_penalty = float(params.get("repetition_penalty", 1.0))
        processors = make_logits_processors(
            repetition_penalty=(repetition_penalty if repetition_penalty != 1.0 else None),
            repetition_context_size=int(params.get("repetition_context_size", 20)),
            presence_penalty=float(params.get("presence_penalty", 0.0)) or None,
            frequency_penalty=float(params.get("frequency_penalty", 0.0)) or None,
        )
        return sampler, list(processors or [])

    def _batch_stop_sequences(self, params: dict):
        """Token-id stop sequences for one request (or None for the defaults).

        mlx-lm's batched generator matches stop sequences on token ids, and the
        model's own EOS/eot ids are its default. An OpenAI ``stop`` string is
        still applied on the text side by the worker's ``StopSequenceFilter``,
        exactly as on the single path, so the two never disagree.
        """
        from mlx_lm.generate import StopSequences

        stop_tokens = getattr(self.processor, "eos_token_ids", None)
        if stop_tokens is None:
            eos = getattr(self.processor, "eos_token_id", None)
            stop_tokens = [eos] if eos is not None else []
        stop_tokens = [int(token) for token in stop_tokens if token is not None]
        if not stop_tokens:
            return None
        return StopSequences([[token] for token in stop_tokens])

    def stream_batched(self, request_id: str, params: dict, prompt: str):
        """Yield worker events for one request served by the batch scheduler.

        The event vocabulary is exactly the one ``stream()`` emits (``delta``,
        ``token_progress``, ``usage``, ``metrics``, ``completed``), so the
        worker's tool splitter, stop filter, heartbeats and memory guard run
        unchanged on top of it. The only new event type is ``batch_fallback``,
        which the server turns into a transparent retry on the single path.
        """
        scheduler = getattr(self, "batch_scheduler", None)
        if scheduler is None:
            yield {"type": "batch_fallback", "reason": "not_capable"}
            return
        try:
            prompt_tokens = self._encode(prompt)
        except Exception as exc:  # noqa: BLE001 - fall back, never fail
            LOGGER.warning("Batched request could not be tokenized: %s", exc)
            yield {"type": "batch_fallback", "reason": "tokenize_failed"}
            return
        if not prompt_tokens:
            yield {"type": "batch_fallback", "reason": "empty_prompt"}
            return
        max_tokens = int(params.get("max_tokens", 512))
        if max_tokens < 1:
            yield {"type": "batch_fallback", "reason": "invalid_max_tokens"}
            return
        try:
            sampler, processors = self._batch_sampler(params)
            stop_sequences = self._batch_stop_sequences(params)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Batched request could not be prepared: %s", exc)
            yield {"type": "batch_fallback", "reason": "sampler_failed"}
            return
        request = BatchRequest(
            request_id=request_id,
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens,
            sampler=sampler,
            logits_processors=processors,
            stop_sequences=stop_sequences,
        )
        try:
            scheduler.start()
        except BatchUnavailable as exc:
            yield {"type": "batch_fallback", "reason": f"start_failed:{exc}"}
            return
        self._batch_requests[request_id] = request
        try:
            scheduler.submit(request)
        except BatchUnavailable as exc:
            self._batch_requests.pop(request_id, None)
            yield {"type": "batch_fallback", "reason": f"submit_failed:{exc}"}
            return
        detokenizer = _BatchDetokenizer(self.processor)
        ticker = _ProgressTicker(params)
        started = time.monotonic()
        generated = 0
        first_token_at: float | None = None
        finish_reason = "stop"
        try:
            while True:
                if request_id in self.cancelled or self.is_aborted(request_id):
                    scheduler.cancel(request_id)
                    yield {"type": "completed", "finish_reason": "cancelled"}
                    return
                try:
                    event = request.events.get(timeout=0.2)
                except queue.Empty:
                    if request.finished:
                        break
                    continue
                kind = event.get("type")
                if kind == "token":
                    generated += 1
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    text = detokenizer.add(event["token"])
                    if text:
                        yield {"type": "delta", "text": text,
                               "tokens": generated,
                               **_batch_rate(generated, first_token_at)}
                    elif ticker.due():
                        yield {"type": "token_progress", "tokens": generated,
                               **_batch_rate(generated, first_token_at)}
                elif kind == "prompt_progress":
                    processed = int(event.get("processed_tokens") or 0)
                    total = int(event.get("total_tokens") or 0)
                    if total and processed < total:
                        yield {"type": "prefill_progress", "processed_tokens": processed,
                               "total_tokens": total}
                elif kind == "finished":
                    finish_reason = str(event.get("finish_reason") or "stop")
                    if finish_reason == "cancelled":
                        yield {"type": "completed", "finish_reason": "cancelled"}
                        return
                    if finish_reason == "error":
                        return
                    break
                elif kind == "error":
                    yield event
                    return
            trailing = detokenizer.flush()
            if trailing:
                yield {"type": "delta", "text": trailing,
                       "tokens": generated, **_batch_rate(generated, first_token_at)}
            if finish_reason == "length" or generated >= max_tokens:
                finish_reason = "length"
            elapsed = max(time.monotonic() - started, 0.001)
            yield {"type": "usage", "prompt_tokens": len(prompt_tokens),
                   "completion_tokens": generated}
            yield {"type": "metrics",
                   "prompt_tokens": len(prompt_tokens),
                   "completion_tokens": generated,
                   "cached_tokens": 0,
                   "cache_tier": "cold",
                   "cold_reason": "continuous_batching",
                   "shared_prefix_tokens": 0,
                   "held_prefix_tokens": len(prompt_tokens),
                   "finish_reason": finish_reason,
                   "tool_support": "none",
                   "prompt_tps": 0.0,
                   "generation_tps": generated / elapsed,
                   "continuous_batching": True}
            yield {"type": "completed", "finish_reason": finish_reason}
        finally:
            self._batch_requests.pop(request_id, None)
            if not request.finished:
                # The consumer went away (disconnect, cancellation, an error in
                # the worker's own post-processing). Remove the sequence so its
                # KV cache is released and the slot is free for the next one.
                scheduler.cancel(request_id)

    def clear_prompt_cache(self) -> None:
        if self.prompt_cache is not None:
            self.prompt_cache.clear_memory()
        super().clear_prompt_cache()

    def clear_disk_prompt_cache(self) -> None:
        if self.prompt_cache is not None:
            self.prompt_cache.clear_disk()

    def clear_paged_prompt_cache(self) -> None:
        if self.prompt_cache is not None:
            self.prompt_cache.clear_paged()

    def prompt_cache_stats(self) -> dict:
        if self.prompt_cache is None:
            return {"enabled": False, "engine": self.engine,
                    "budget": dict(self.cache_budget), "lastColdReason": self.last_cold_reason,
                    "kvQuant": self._kv_quant_status()}
        result = self.prompt_cache.stats()
        result["budget"] = dict(self.cache_budget)
        result["lastColdReason"] = self.last_cold_reason
        result["rollbackCapability"] = cache_state.ROLLBACK_TRIM
        result["kvQuant"] = self._kv_quant_status()
        # v5.1.0: the coordinator decides whether to route a second request to
        # this worker from this flag, so it must reflect the loaded model, not
        # the settings.
        result["continuousBatching"] = self.batch_status()
        return result

    def _encode(self, text: str) -> list[int]:
        """Tokenize exactly as `stream_generate` would for the same string."""
        tokenizer = self.processor
        bos = getattr(tokenizer, "bos_token", None)
        add_special_tokens = bos is None or not text.startswith(bos)
        return list(tokenizer.encode(text, add_special_tokens=add_special_tokens))

    def _render_prompt(self, params: dict) -> str:
        prompt = params.get("messages", params.get("prompt", ""))
        if not isinstance(prompt, list):
            return str(prompt)
        prompt = normalize_tool_call_messages(prompt)
        last_error: Exception | None = None
        for extra_kwargs in tool_template_kwargs_attempts(params):
            try:
                rendered = self.processor.apply_chat_template(
                    prompt, tokenize=False, add_generation_prompt=True, **extra_kwargs)
                return rendered
            except Exception as exc:  # noqa: BLE001 - mirror stream()'s fallback
                last_error = exc
        raise last_error if last_error else RuntimeError("could not render prompt")

    def count_tokens(self, params: dict) -> int:
        if self.model is None:
            raise RuntimeError("model is not loaded")
        return len(self._encode(self._render_prompt(params)))

    def stream(self, request_id: str, params: dict):
        if self.model is None:
            raise RuntimeError("model is not loaded")
        from mlx_lm import stream_generate
        prompt = params.get("messages", params.get("prompt", ""))
        tool_support = "none"
        if isinstance(prompt, list):
            prompt = normalize_tool_call_messages(prompt)
            last_error: Exception | None = None
            for extra_kwargs in tool_template_kwargs_attempts(params):
                try:
                    prompt = self.processor.apply_chat_template(
                        prompt, tokenize=False, add_generation_prompt=True, **extra_kwargs
                    )
                    last_error = None
                    tool_support = _tool_support(params, extra_kwargs)
                    break
                except Exception as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
        if tool_support == "degraded":
            # The template could not be rendered with `tools`, so the model was
            # never told the tools exist. Without this the caller only sees a
            # model that mysteriously refuses to call anything.
            yield {"type": "tool_support", "state": "degraded"}
        max_tokens = int(params.get("max_tokens", 512))
        kwargs = {"max_tokens": max_tokens}
        kwargs.update(self._kv_generate_kwargs())
        quantized_active = self.kv_bits is not None
        temperature = float(params.get("temperature", 0.7))
        top_p = float(params.get("top_p", 1.0))
        try:
            from mlx_lm.sample_utils import make_logits_processors, make_sampler
            kwargs["sampler"] = make_sampler(temp=temperature, top_p=top_p)
            repetition_penalty = float(params.get("repetition_penalty", 1.0))
            processors = make_logits_processors(
                repetition_penalty=(repetition_penalty if repetition_penalty != 1.0 else None),
                repetition_context_size=int(params.get("repetition_context_size", 20)),
                presence_penalty=float(params.get("presence_penalty", 0.0)) or None,
                frequency_penalty=float(params.get("frequency_penalty", 0.0)) or None,
            )
            if processors:
                kwargs["logits_processors"] = processors
        except Exception:
            kwargs["temp"] = temperature
        seed = params.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            try:
                import mlx.core as mx
                mx.random.seed(seed)
            except Exception:
                pass

        prompt_tokens: list[int] = []
        cache = None
        cache_tier = "cold"
        adaptive_active = False
        adaptive_cfg = params.get("adaptiveMemory") or {}
        if isinstance(prompt, str) and adaptive_cfg.get("enabled") and adaptive_cfg.get("softToken"):
            # Experimental soft-token Hybrid Prefiller. Everything here happens
            # before a single token is yielded, so any failure falls straight
            # through to the ordinary EXACT path below with nothing emitted.
            try:
                from .adaptive_memory import AdaptiveRuntime, FallbackToExact
                adaptive_tokens = self._encode(prompt)
                runtime = AdaptiveRuntime(
                    self.model, self.processor, adaptive_cfg,
                    cache_root=os.environ.get("MLXBAR_PROMPT_CACHE_ROOT"))
                plan = runtime.prepare(adaptive_tokens)
                cache = plan["cache"]
                kwargs["prompt"] = plan["remaining"]
                kwargs["prompt_cache"] = cache
                prompt_tokens = plan["all_tokens"]
                cache_tier = "adaptive"
                adaptive_active = True
                yield {"type": "metrics", "adaptive_memory": plan["metrics"]}
            except FallbackToExact as exc:
                LOGGER.info("adaptive memory fell back to EXACT: %s", exc)
                cache = None
                cache_tier = "cold"
                kwargs.pop("prompt_cache", None)
            except Exception as exc:  # noqa: BLE001 - never break a request
                LOGGER.warning("adaptive memory disabled for this request: %s", exc)
                cache = None
                cache_tier = "cold"
                kwargs.pop("prompt_cache", None)
        if not adaptive_active and isinstance(prompt, str) and self.prompt_cache is not None:
            try:
                prompt_tokens = self._encode(prompt)
            except Exception:
                prompt_tokens = []
        if adaptive_active:
            pass
        elif prompt_tokens:
            cache, remaining, cache_tier = self.prompt_cache.fetch(self.model, prompt_tokens)
            if not self._cache_matches_kv_mode(cache):
                # Mode changed without a worker restart, or a stale snapshot
                # from another mode slipped through: never feed a quantized
                # cache to a bf16 request (or vice versa with wrong bits).
                try:
                    from mlx_lm.models.cache import make_prompt_cache
                    cache = make_prompt_cache(self.model)
                except Exception:
                    cache = None
                remaining = prompt_tokens
                cache_tier = "cold"
            if cache is None:
                try:
                    from mlx_lm.models.cache import make_prompt_cache
                    cache = make_prompt_cache(self.model)
                except Exception:
                    cache = None
                remaining = prompt_tokens
            kwargs["prompt"] = remaining
            if cache is not None:
                kwargs["prompt_cache"] = cache
        else:
            kwargs["prompt"] = prompt

        if prompt_tokens and self._cold_prompt_tps > 0 and len(prompt_tokens) >= 1024:
            # The exact prompt length is already known here, so this estimate
            # only has to guess the rate.
            yield {"type": "prefill_estimate",
                   "estimated_prompt_tokens": len(prompt_tokens),
                   "estimated_seconds": round(len(prompt_tokens) / self._cold_prompt_tps, 1)}
        # A template that pre-opens `<think>` in the prompt makes the model emit
        # only the closing `</think>`; tell the splitter it starts inside a
        # reasoning block. Needed with or without tools.
        if isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        generated: list[int] = []
        completed = False
        finish_reason = None
        ticker = _ProgressTicker(params)
        try:
            retried_exact = False
            while True:
                saw_model_event = False
                try:
                    for response in stream_generate(self.model, self.processor, **kwargs):
                        # Once the runtime returns any model event, replay is no
                        # longer safe even if its text is empty: sampling state
                        # and the live cache may already have advanced.
                        saw_model_event = True
                        token = getattr(response, "token", None)
                        if isinstance(token, int) and not isinstance(token, bool):
                            generated.append(token)
                        if request_id in self.cancelled:
                            return
                        last_response = response
                        finish_reason = getattr(response, "finish_reason", None) or finish_reason
                        text = getattr(response, "text", response if isinstance(response, str) else "")
                        if text:
                            yield {"type": "delta", "text": text, **_live_progress(response)}
                        elif ticker.due():
                            yield {"type": "token_progress", **_live_progress(response)}
                    completed = True
                    break
                except Exception as exc:
                    paged_failed = (cache_tier == "paged_disk" and not saw_model_event
                                    and not retried_exact and request_id not in self.cancelled)
                    quant_failed = (quantized_active and not saw_model_event
                                    and not retried_exact and request_id not in self.cancelled)
                    if not (paged_failed or quant_failed):
                        raise
                    # A restored/quantized cache is an optimization only. Before
                    # the first model event, discard it and replay exactly once
                    # from a runtime-created empty cache. No public delta can
                    # duplicate.
                    if paged_failed:
                        LOGGER.warning("Paged KV failed before first output; retrying EXACT: %s", exc)
                        if self.prompt_cache is not None:
                            self.prompt_cache.mark_paged_fallback(
                                f"first_evaluation:{type(exc).__name__}")
                    else:
                        LOGGER.warning("Quantized KV failed before first output; retrying bf16: %s", exc)
                        self.kv_fallback_reason = f"first_evaluation:{type(exc).__name__}"
                        for key in ("kv_bits", "kv_group_size", "quantized_kv_start"):
                            kwargs.pop(key, None)
                        quantized_active = False
                    from mlx_lm.models.cache import make_prompt_cache
                    cache = make_prompt_cache(self.model)
                    kwargs["prompt"] = prompt_tokens
                    kwargs["prompt_cache"] = cache
                    cache_tier = "cold"
                    generated.clear()
                    last_response = None
                    finish_reason = None
                    retried_exact = True
                    if isinstance(seed, int) and not isinstance(seed, bool):
                        with contextlib.suppress(Exception):
                            import mlx.core as mx
                            mx.random.seed(seed)
        finally:
            # The warm tier hands out copies, so an abandoned cache can never
            # corrupt a stored one. That makes an interrupted turn worth keeping
            # too: its cache holds a real prefix of the conversation, and
            # throwing it away is what used to make the next request pay for the
            # whole prompt again. Only the pairing has to be proven first.
            if (cache is not None and prompt_tokens and self.prompt_cache is not None
                    and not adaptive_active):
                # A hybrid (soft-token) cache is not a plain prefix of the raw
                # prompt, so it must never be stored in the EXACT prompt cache.
                self._remember(cache, prompt_tokens, generated, completed, request_id)

        if len(generated) >= max_tokens:
            # The runtime reports this only on some versions; the token count is
            # authoritative and lets a client tell a cut-off reply from a
            # finished one.
            finish_reason = "length"
        if (last_response is not None and not isinstance(last_response, str)
                and hasattr(last_response, "prompt_tokens")):
            total_prompt = len(prompt_tokens) or int(getattr(last_response, "prompt_tokens", 0) or 0)
            processed = int(getattr(last_response, "prompt_tokens", 0) or 0)
            cached = max(0, total_prompt - processed) if prompt_tokens else 0
            yield {"type": "usage", "prompt_tokens": total_prompt,
                   "completion_tokens": int(getattr(last_response, "generation_tokens", 0) or 0)}
            effective_tier = cache_tier if cached else "cold"
            rate = float(getattr(last_response, "prompt_tps", 0.0) or 0.0)
            if not cached and rate > 0:
                self._cold_prompt_tps = rate
            yield {"type": "metrics",
                   "prompt_tokens": total_prompt,
                   "cached_tokens": cached,
                   "cache_tier": effective_tier,
                   "cold_reason": self._cold_reason(effective_tier),
                   "shared_prefix_tokens": cached,
                   "held_prefix_tokens": total_prompt,
                   "finish_reason": finish_reason,
                   "tool_support": tool_support,
                   "prompt_tps": float(getattr(last_response, "prompt_tps", 0.0) or 0.0),
                   "generation_tps": float(getattr(last_response, "generation_tps", 0.0) or 0.0)}
        elif finish_reason:
            yield {"type": "metrics", "finish_reason": finish_reason}

    def _cache_matches_kv_mode(self, cache) -> bool:
        """Whether a reused cache belongs to the latched quantization mode.

        A quantized cache carries a `bits` attribute on its layers; a
        full-precision cache does not. Mixing them would silently run the
        wrong attention path, so a mismatch is treated as a cold miss.
        """
        if cache is None:
            return True
        try:
            layers = list(cache)
        except TypeError:
            return False
        if not layers:
            return True
        if self.kv_bits is None:
            return not any(hasattr(layer, "bits") for layer in layers)
        for layer in layers:
            bits = getattr(layer, "bits", None)
            group = getattr(layer, "group_size", None)
            if bits is None:
                continue
            if int(bits) != int(self.kv_bits) or int(group or 0) != int(self.kv_group_size):
                return False
        return True

    def _remember(self, cache, prompt_tokens: list[int], generated: list[int],
                  completed: bool, request_id: str) -> None:
        if completed:
            self.prompt_cache.store(self.model, prompt_tokens + generated, cache,
                                    prompt_length=len(prompt_tokens))
            return
        if self.abort_reason(request_id) == cache_state.COLD_MEMORY_PRESSURE:
            # This interruption exists to release memory, so retaining a cache
            # would undo it.
            self._pending_cold_reason = cache_state.COLD_MEMORY_PRESSURE
            return
        expected = len(prompt_tokens) + len(generated)
        observed = cache_state.cached_length(cache)
        if observed is None or observed != expected:
            self._pending_cold_reason = (cache_state.COLD_TOKEN_IDS_UNAVAILABLE
                                         if observed is None
                                         else cache_state.COLD_CANCELLED_PREVIOUS)
            return
        # Persisting stops at the prompt, as it does for a completed turn: only
        # a prefix a later request can share is worth a gigabyte of disk.
        self.prompt_cache.store(self.model, prompt_tokens + generated, cache,
                                prompt_length=len(prompt_tokens))

    def _cold_reason(self, cache_tier: str) -> str | None:
        if cache_tier != "cold":
            self.last_cold_reason = None
            self._pending_cold_reason = None
            return None
        pending, self._pending_cold_reason = self._pending_cold_reason, None
        reason = pending or cache_state.COLD_NO_PREFIX
        if not pending and self.prompt_cache is not None:
            disabled = getattr(self.prompt_cache, "disabled_reason", None)
            if disabled in cache_state.COLD_REASONS:
                reason = disabled
        self.last_cold_reason = reason
        return reason

    def finalize(self, text: str, params: dict) -> dict:
        tools = params.get("tools") or []
        try:
            from mlx_lm.tool_parsers import _infer_tool_parser_from_tokenizer, load_tool_module
            from mlx_lm.server import process_tool_calls
            parser_type = _infer_tool_parser_from_tokenizer(self.processor)
            if parser_type:
                parsed = process_tool_calls(text, load_tool_module(parser_type), tools)
                calls = parsed.get("calls") or []
                if calls:
                    return {"text": parsed.get("remaining_text", ""), "tool_calls": calls}
        except Exception:
            pass
        return parse_tool_markup(text)


def _read_cache_budget(path: str) -> dict:
    """Cache size arithmetic for this model, read from its own config."""
    import json
    from pathlib import Path
    try:
        config = json.loads((Path(path) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"known": False}
    return cache_state.model_cache_budget(config)


class _ProgressTicker:
    """Throttled per-token progress, independent of when text is emitted.

    A runtime only yields a `delta` when the detokenizer has a complete
    segment, and some models hand over a whole reply at once after many
    seconds. Counting deltas would then report nothing at all, so the tick is
    driven by the generation loop itself rather than by visible output.
    """

    def __init__(self, params: dict):
        try:
            self.interval = min(30.0, max(0.5, float(
                params.get("heartbeat_interval_seconds", 10))))
        except (TypeError, ValueError):
            self.interval = 10.0
        self.last = time.monotonic()

    def due(self) -> bool:
        now = time.monotonic()
        if now - self.last < self.interval:
            return False
        self.last = now
        return True


def _live_progress(response) -> dict:
    """Per-token counters the runtime already computes, if it still offers them.

    Both mlx-lm and mlx-vlm set `generation_tokens` and `generation_tps` on
    every streamed response, and their tps excludes prefill. Reading them costs
    nothing and is more accurate than counting deltas, but a future runtime may
    rename or drop them, so absence is normal rather than an error -- the
    worker falls back to its own count.
    """
    result = {}
    tokens = getattr(response, "generation_tokens", None)
    if isinstance(tokens, int) and tokens >= 0:
        result["tokens"] = tokens
    tps = getattr(response, "generation_tps", None)
    if isinstance(tps, (int, float)) and tps > 0:
        result["tps"] = float(tps)
    return result


def _batch_rate(generated: int, first_token_at: float | None) -> dict:
    """Live decode rate for a batched request (prefill excluded)."""
    if first_token_at is None or generated <= 0:
        return {}
    span = time.monotonic() - first_token_at
    if span <= 0.05:
        return {}
    return {"tps": generated / span}


class _BatchDetokenizer:
    """Incremental detokenizer for token ids produced by the batch scheduler.

    ``stream_generate`` owns its own detokenizer; the batched path receives raw
    token ids, so this reproduces the same visible text: decode the whole
    sequence so far and emit only the new suffix, which keeps multi-byte
    characters from being split across deltas. A tokenizer without ``decode``
    (or one that raises) degrades to "no text", never to an error.
    """

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.tokens: list[int] = []
        self.text = ""

    def add(self, token: int) -> str:
        self.tokens.append(int(token))
        decode = getattr(self.tokenizer, "decode", None)
        if not callable(decode):
            return ""
        try:
            full = decode(self.tokens)
        except Exception:  # noqa: BLE001 - never fail a generation on decoding
            return ""
        if not isinstance(full, str) or not full.startswith(self.text):
            # The incremental assumption no longer holds (replacement text or
            # a rewritten prefix). Resynchronise rather than emit garbage.
            self.text = full if isinstance(full, str) else self.text
            return ""
        new = full[len(self.text):]
        self.text = full
        return new

    def flush(self) -> str:
        return ""


def _tool_support(params: dict, rendered_kwargs: dict) -> str:
    """Whether the rendered template actually carried the requested tools."""
    if not params.get("tools") or params.get("tool_choice") == "none":
        return "none"
    return "full" if rendered_kwargs.get("tools") else "degraded"


if __name__ == "__main__":
    run(MLXLMAdapter())
