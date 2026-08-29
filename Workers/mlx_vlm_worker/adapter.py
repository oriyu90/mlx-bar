from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import linecache
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from common import cache_state
from common.server import BaseAdapter, memory_pressure_reason, run
from common.tool_calls import parse_tool_markup, tool_template_kwargs_attempts

from .prompt_cache import CheckpointStore, build_guarded_state

LOGGER = logging.getLogger(__name__)


# Every APC cache generation is a directory under `apc_root` whose name starts
# with this. The checkpoint store keeps its own generations under a sibling
# `checkpoints/` directory, so the sweep below has to recognise which of the two
# it is looking at rather than treating every directory as a stale generation.
APC_NAMESPACE_PREFIX = "mlxbar-vlm-v1-"


class MLXVLMAdapter(BaseAdapter):
    engine = "mlx-vlm"

    def __init__(self):
        super().__init__()
        self.modalities = ["text", "image"]
        self.prompt_cache_state = None
        self.apc_manager = None
        self.apc_root: Path | None = None
        self.apc_namespace: str | None = None
        self.apc_disabled_reason: str | None = None
        self.model_path: str | None = None
        self.prompt_cache_reuse_failures = 0
        self.checkpoints: CheckpointStore | None = None
        self.cache_budget: dict = {"known": False}
        self.rollback_capability: str = cache_state.ROLLBACK_NONE
        self.last_cold_reason: str | None = None
        self._pending_cold_reason: str | None = None
        self.prompt_growth = 0
        self._pending_prompt_ids: list[int] | None = None
        self._previous_prompt_tokens = 0
        self._persisted_tokens = 0
        self._chars_per_token = 0.0
        self._cold_prompt_tps = 0.0

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["modalities"] = self.modalities
        result["promptCaching"] = self.prompt_cache_state is not None
        result["promptCache"] = self.prompt_cache_stats()
        result["cacheBudget"] = dict(self.cache_budget)
        result["rollbackCapability"] = self.rollback_capability
        return result

    @staticmethod
    def _truthy(value: str | None) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _model_fingerprint(path: Path, runtime_version: str) -> str:
        """Fingerprint cache compatibility without reading multi-GB weights."""
        digest = hashlib.sha256()
        digest.update(b"mlxbar-vlm-apc-v1\0")
        digest.update(str(path.resolve()).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(runtime_version.encode("ascii", errors="replace"))
        metadata_names = {
            "config.json", "tokenizer.json", "tokenizer_config.json",
            "processor_config.json", "preprocessor_config.json",
            "chat_template.json", "chat_template.jinja",
            "model.safetensors.index.json",
        }
        entries = []
        try:
            entries = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in metadata_names:
                try:
                    digest.update(entry.name.encode("utf-8"))
                    digest.update(entry.read_bytes())
                except OSError:
                    continue
            elif entry.suffix == ".safetensors":
                try:
                    stat = entry.stat()
                    digest.update(entry.name.encode("utf-8"))
                    digest.update(str(stat.st_size).encode("ascii"))
                    digest.update(str(stat.st_mtime_ns).encode("ascii"))
                except OSError:
                    continue
        return digest.hexdigest()

    def _close_apc(self) -> None:
        manager, self.apc_manager = self.apc_manager, None
        if manager is not None:
            try:
                manager.close()
            except Exception as exc:
                logging.getLogger(__name__).warning("Disk APC shutdown failed: %s", exc)

    def _init_apc(self, model_path: str) -> None:
        self._close_apc()
        self.apc_root = None
        self.apc_namespace = None
        self.apc_disabled_reason = None
        if not self._truthy(os.environ.get("MLXBAR_PROMPT_CACHE_DISK_ENABLED", "1")):
            self.apc_disabled_reason = "disabled_by_setting"
            return
        root_value = os.environ.get("MLXBAR_PROMPT_CACHE_ROOT")
        if not root_value:
            self.apc_disabled_reason = "cache_root_missing"
            return
        try:
            from mlx_vlm.apc import APCManager, DiskBlockStore
            os.environ.setdefault("APC_HASH", "sha256")
            os.environ.setdefault("APC_EXACT_CACHE_ENTRIES", "0")
            os.environ.setdefault("APC_EXACT_PREFIX_GUARD_TOKENS", "256")
            runtime_version = importlib.metadata.version("mlx-vlm")
            fingerprint = self._model_fingerprint(Path(model_path), runtime_version)
            self.apc_root = Path(root_value)
            self.apc_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.apc_root, 0o700)
            self.apc_namespace = APC_NAMESPACE_PREFIX + fingerprint
            maximum = int(os.environ.get("MLXBAR_PROMPT_CACHE_MAX_BYTES", str(5 << 30)))
            disk = DiskBlockStore(
                self.apc_root,
                namespace=self.apc_namespace,
                max_bytes=max(0, maximum),
            )
            # PromptCacheState remains the warm-memory tier. A zero-block APC
            # manager adds only persistent disk reuse and avoids duplicating a
            # large Qwen hybrid cache in unified memory. The pool can be turned
            # on from settings, but it stays off by default: nobody has measured
            # what it does to a 27B-class hybrid, and an unmeasured default is
            # not a default worth shipping.
            self.apc_manager = APCManager(num_blocks=self._apc_block_count(), disk=disk)
            os.chmod(disk.dir, 0o700)
            try:
                keep = int(os.environ.get("MLXBAR_PROMPT_CACHE_KEEP_GENERATIONS", "2"))
            except (TypeError, ValueError):
                keep = 2
            self._sweep_stale_namespaces(min(10, max(1, keep)))
        except Exception as exc:
            self._close_apc()
            self.apc_disabled_reason = f"initialization_failed:{type(exc).__name__}"
            logging.getLogger(__name__).warning("Disk APC disabled: %s", exc)

    def _tune_apc_guard(self) -> None:
        """Keep APC's exact-prefix guard wider than one turn of growth.

        APC stores a snapshot of everything but the last `guard` tokens, so the
        guard has to be at least as large as the amount a client adds between
        turns -- otherwise the stored prefix ends inside the previous turn and
        never matches again. A fixed 256 fits a chat client and does not fit an
        agent that appends tool results, so it is measured instead of assumed.
        """
        manager = self.apc_manager
        if manager is None or not hasattr(manager, "exact_cache_guard_tokens"):
            return
        if self.prompt_growth <= 0:
            return
        with contextlib.suppress(Exception):
            manager.exact_cache_guard_tokens = max(256, min(16384, self.prompt_growth * 2))

    def _apc_block_count(self) -> int:
        """How many APC blocks the memory budget can pay for.

        Derived rather than fixed: a block holds a fixed number of *tokens*, and
        what a token costs is a property of the model, not of MLXBar.
        """
        setting = str(os.environ.get("MLXBAR_APC_MEMORY_BLOCKS", "0")).strip().lower()
        if setting != "auto":
            try:
                return max(0, int(setting))
            except (TypeError, ValueError):
                return 0
        budget = self.cache_budget
        if not budget.get("known"):
            return 0
        try:
            ratio = float(os.environ.get("MLXBAR_PROMPT_CACHE_MEMORY_RATIO", "0.10"))
        except (TypeError, ValueError):
            ratio = 0.10
        try:
            physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            return 0
        try:
            from mlx_vlm.apc import DEFAULT_BLOCK_SIZE
            block_size = int(DEFAULT_BLOCK_SIZE)
        except Exception:
            return 0
        if block_size <= 0 or ratio <= 0:
            return 0
        tokens = cache_state.affordable_tokens(budget, int(physical * ratio))
        return max(0, min(65536, tokens // block_size))

    def _sweep_stale_namespaces(self, keep: int) -> list[str]:
        """Delete cache generations this model/runtime can no longer read.

        The namespace fingerprints the model files *and* the mlx-vlm version,
        so every model switch and every runtime update starts a fresh one. The
        `max_bytes` budget only bounds a single namespace, so without this the
        directory grows by that budget per generation and nothing ever reclaims
        it. The current namespace is always kept; older ones survive only up to
        `keep` so returning to a previous model can still hit its cache.
        """
        if self.apc_root is None or not self.apc_namespace:
            return []
        try:
            entries = [item for item in self.apc_root.iterdir()
                       if item.is_dir() and item.name != self.apc_namespace
                       and item.name.startswith(APC_NAMESPACE_PREFIX)]
        except OSError:
            return []

        def modified(item: Path) -> float:
            try:
                return item.stat().st_mtime
            except OSError:
                return 0.0

        entries.sort(key=modified, reverse=True)
        removed = []
        for stale in entries[max(0, keep - 1):]:
            try:
                shutil.rmtree(stale)
                removed.append(stale.name)
            except OSError as exc:
                logging.getLogger(__name__).warning("Stale cache sweep failed for %s: %s",
                                                    stale.name, exc)
        if removed:
            logging.getLogger(__name__).info("Removed %d stale prompt-cache generation(s)",
                                             len(removed))
        return removed

    def _disable_apc_after_failure(self, exc: Exception) -> None:
        self._close_apc()
        self.apc_disabled_reason = f"runtime_failed:{type(exc).__name__}"
        logging.getLogger(__name__).warning(
            "Disk APC failed; retrying with PromptCacheState only: %s", exc
        )

    @staticmethod
    def _is_cache_reuse_failure(exc: Exception) -> bool:
        """True only when the failure came from rolling a cache back.

        mlx-vlm rolls a retained cache back to the shared prefix by calling
        ``trim()`` on every entry, guarded by a retention check rather than by
        ``is_trimmable()``. Architectures whose cache list holds an entry with
        no ``trim`` method at all -- hybrid Qwen3.5/3.8 layers use
        ``ArraysCache`` -- therefore raise instead of falling back to a cold
        prefill. Reuse is an optimisation, so retry once with a fresh cache.

        The test has to be the failing *call*, not the module it happened in:
        ``stream_generate`` itself lives in ``generate/dispatch.py``, so every
        generation error whatsoever passes through a frame there. Matching the
        module alone would retry genuine model errors -- wasting a prefill and,
        worse, discarding the warm cache over an unrelated failure.
        """
        trace = exc.__traceback__
        while trace is not None:
            filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
            if "/mlx_vlm/" in filename.lower():
                # Reading the failing source line keeps this independent of
                # line numbers, which move between runtime versions.
                if ".trim(" in linecache.getline(filename, trace.tb_lineno):
                    return True
            trace = trace.tb_next
        # Source may be unavailable; the missing-method shape is unambiguous.
        return isinstance(exc, AttributeError) and "trim" in str(exc)

    @staticmethod
    def _is_apc_failure(exc: Exception) -> bool:
        """True only when the failure actually came from the APC code.

        The traceback is the strongest evidence, but an APC error re-raised
        from outside the module loses those frames, so unambiguous message
        markers still count. "safetensors" is no longer one of them: ordinary
        weight-loading errors mention it too, and a false positive here
        disables the disk cache for the rest of the session.
        """
        trace = exc.__traceback__
        while trace is not None:
            filename = trace.tb_frame.f_code.co_filename.replace("\\", "/").lower()
            if "/mlx_vlm/apc" in filename:
                return True
            trace = trace.tb_next
        message = str(exc).lower()
        return any(marker in message for marker in (" apc", "apc ", "prefix cache", "cache snapshot"))

    def prompt_cache_stats(self) -> dict:
        result = {
            "enabled": self.prompt_cache_state is not None or self.apc_manager is not None,
            "engine": self.engine,
            "memory": self.prompt_cache_state is not None,
            "disk": self.apc_manager is not None,
            "namespace": self.apc_namespace,
            "disabledReason": self.apc_disabled_reason,
            "generations": self._namespace_count(),
            "diskBytes": self._namespace_bytes(),
            "reuseFailures": self.prompt_cache_reuse_failures,
            "rollbackCapability": self.rollback_capability,
            "budget": dict(self.cache_budget),
            "lastColdReason": self.last_cold_reason,
            "promptGrowthTokens": self.prompt_growth,
        }
        if self.checkpoints is not None:
            result["checkpoint"] = self.checkpoints.stats()
        if self.apc_manager is not None:
            try:
                result.update(self.apc_manager.stats_snapshot())
            except Exception as exc:
                result["statsError"] = type(exc).__name__
        return result

    def _namespace_count(self) -> int:
        if self.apc_root is None:
            return 0
        try:
            return sum(1 for item in self.apc_root.iterdir()
                       if item.is_dir() and item.name.startswith(APC_NAMESPACE_PREFIX))
        except OSError:
            return 0

    def _namespace_bytes(self) -> int:
        total = 0
        if self.apc_root is None:
            return total
        try:
            for item in self.apc_root.rglob("*"):
                with contextlib.suppress(OSError):
                    if item.is_file():
                        total += item.stat().st_size
        except OSError:
            return total
        return total

    def _apc_stats_snapshot(self) -> dict:
        """Keep optional diagnostics from becoming a generation dependency."""
        if self.apc_manager is None:
            return {}
        try:
            return self.apc_manager.stats_snapshot()
        except Exception:
            return {}

    def _reset_prompt_cache(self) -> None:
        try:
            from mlx_vlm.generate import PromptCacheState
        except (ImportError, AttributeError):
            # Older runtime slots remain usable without the optimization.
            self.prompt_cache_state = None
            return
        self._pending_prompt_ids = None
        try:
            self.prompt_cache_state = build_guarded_state(PromptCacheState, self)
        except Exception as exc:
            # A runtime whose PromptCacheState cannot be subclassed still works;
            # it just loses the rollback guard and keeps the old behaviour.
            LOGGER.warning("Falling back to the runtime prompt cache state: %s", exc)
            self.prompt_cache_state = PromptCacheState()

    def _make_cache(self):
        """Build an empty cache shaped like the one the runtime uses.

        mlx-vlm builds its cache from `model.language_model`, so a snapshot
        captured during generation only restores into a cache made the same
        way. Mirroring the runtime's own call keeps the component classes and
        their order identical without copying its internals.
        """
        from mlx_vlm.models import cache as runtime_cache
        target = getattr(self.model, "language_model", self.model)
        return runtime_cache.make_prompt_cache(target)

    def _probe_rollback_capability(self) -> str:
        """Ask an empty cache what kind of rollback this architecture supports.

        Done once at load, on an empty cache, so it costs nothing and the answer
        is available before the first request rather than after the first
        failure. An architecture that gains `trim` in a later runtime is picked
        up here automatically.
        """
        self.rollback_capability = cache_state.ROLLBACK_NONE
        try:
            probe = self._make_cache()
        except Exception as exc:
            LOGGER.warning("Could not probe cache rollback support: %s", exc)
            return self.rollback_capability
        self.rollback_capability = cache_state.rollback_capability(probe)
        return self.rollback_capability

    def _init_checkpoints(self, model_path: str) -> None:
        """Prepare the boundary snapshot store for architectures that need it.

        Only an architecture that cannot trim needs snapshots: one that can roll
        its cache back in place already has a cheaper route to the same result,
        and capturing for it would double the cache in memory for nothing.
        """
        self.checkpoints = None
        if self.rollback_capability != cache_state.ROLLBACK_CHECKPOINT:
            return
        if not self._truthy(os.environ.get("MLXBAR_PROMPT_CACHE_CHECKPOINT", "1")):
            return
        root_value = os.environ.get("MLXBAR_PROMPT_CACHE_ROOT")
        try:
            maximum = int(os.environ.get("MLXBAR_PROMPT_CACHE_MAX_BYTES", str(5 << 30)))
        except (TypeError, ValueError):
            maximum = 5 << 30
        try:
            keep = int(os.environ.get("MLXBAR_PROMPT_CACHE_KEEP_GENERATIONS", "2"))
        except (TypeError, ValueError):
            keep = 2
        try:
            write_budget = int(os.environ.get("MLXBAR_PROMPT_CACHE_WRITE_BUDGET_BYTES",
                                              str(32 << 30)))
        except (TypeError, ValueError):
            write_budget = 32 << 30
        try:
            runtime_version = importlib.metadata.version("mlx-vlm")
        except Exception:
            runtime_version = "unknown"
        namespace = f"mlxbar-vlm-ckpt-v1-{self._model_fingerprint(Path(model_path), runtime_version)}"
        disk_enabled = self._truthy(os.environ.get("MLXBAR_PROMPT_CACHE_DISK_ENABLED", "1"))
        self.checkpoints = CheckpointStore(
            root=(Path(root_value) / "checkpoints") if root_value else None,
            namespace=namespace,
            budget=self.cache_budget,
            max_bytes=maximum,
            keep_generations=min(10, max(1, keep)),
            write_budget_bytes=write_budget,
            disk_enabled=disk_enabled,
        )

    # ------------------------------------------------- guarded state hooks

    def observe_prompt(self, token_ids: list[int]) -> None:
        """Record the prompt the runtime actually tokenised.

        This is the only place the exact token ids of the current prompt are
        available: the runtime tokenises inside `stream_generate`, and it only
        writes them back into the cache state when a generation completes. A
        cancelled turn therefore has no other source for them, and re-encoding
        the prompt text would not do -- a tokenizer does not guarantee that
        decode/encode round-trips to the same ids.
        """
        self.prompt_growth = max(0, len(token_ids) - self._previous_prompt_tokens)
        self._previous_prompt_tokens = len(token_ids)
        self._pending_prompt_ids = list(token_ids)

    def restore_for(self, token_ids: list[int]):
        if self.checkpoints is None:
            return None
        try:
            return self.checkpoints.restore_best(token_ids, self._make_cache)
        except Exception as exc:
            LOGGER.warning("Checkpoint restore failed; continuing cold: %s", exc)
            return None

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        if not path:
            raise ValueError("model path is required")
        self.apply_memory_limits()
        config = json.loads((Path(path) / "config.json").read_text(encoding="utf-8"))
        has_visual_input = any(config.get(key) not in (None, {}) for key in (
            "vision_config", "vision_tower", "visual", "image_token_id",
        ))
        self.modalities = ["text", "image"] if has_visual_input else ["text"]
        from mlx_vlm import load
        self.model, self.processor = load(path, trust_remote_code=trust_remote_code)
        self.model_path = path
        self.apply_memory_limits()
        self._reset_prompt_cache()
        # Order matters: the cache budget and the rollback probe are inputs to
        # how the APC pool and the checkpoint store are sized.
        self.cache_budget = cache_state.model_cache_budget(config)
        self._probe_rollback_capability()
        self._init_apc(path)
        self._init_checkpoints(path)
        self._log_cache_plan()
        return self.capabilities()

    def _log_cache_plan(self) -> None:
        """Say up front what reuse this model will get, and what it will cost.

        The failure this exists to prevent is silent: before v1.6.0 a model
        whose snapshots did not fit the disk budget kept writing and evicting
        them, so the cache looked enabled while never returning more than its
        first small prefix. Stating the arithmetic at load turns that into
        something visible in one line.
        """
        budget = self.cache_budget
        if not budget.get("known"):
            LOGGER.info("Prompt cache: rollback=%s, size per token unknown for this config",
                        self.rollback_capability)
            return
        per_token = int(budget.get("perTokenBytes", 0))
        affordable = self.checkpoints.affordable_tokens() if self.checkpoints else 0
        LOGGER.info(
            "Prompt cache: rollback=%s, %.1f KB/token, snapshots affordable up to %d tokens",
            self.rollback_capability, per_token / 1024.0, affordable)

    def unload(self) -> None:
        self._close_apc()
        if self.checkpoints is not None:
            self.checkpoints.forget()
        self.checkpoints = None
        self.prompt_cache_state = None
        self.model_path = None
        self.cache_budget = {"known": False}
        self.rollback_capability = cache_state.ROLLBACK_NONE
        self._previous_prompt_tokens = 0
        self._persisted_tokens = 0
        super().unload()

    def clear_prompt_cache(self) -> None:
        self.prompt_cache_state = None
        if self.checkpoints is not None:
            # The in-memory snapshot is a second full copy of the cache, so it
            # has to go when the caller is asking for memory back. The disk tier
            # survives: it costs no RAM and is what avoids a cold start later.
            self.checkpoints.forget()
        super().clear_prompt_cache()
        self._reset_prompt_cache()

    def clear_disk_prompt_cache(self) -> None:
        root = self.apc_root
        namespace = self.apc_namespace
        model_path = self.model_path
        self._close_apc()
        cache_directory = root / namespace if root is not None and namespace else None
        if cache_directory is not None and cache_directory.exists():
            shutil.rmtree(cache_directory)
        if self.checkpoints is not None:
            self.checkpoints.forget()
            self.checkpoints.clear_disk()
        if model_path is not None:
            self._init_apc(model_path)
            self._init_checkpoints(model_path)

    def count_tokens(self, params: dict) -> int:
        """Best-effort exact count for the Anthropic count_tokens endpoint.

        Renders the chat template exactly as stream() would, then tokenises with
        whatever encoder the runtime exposes. Image contribution is not modelled
        (Anthropic's own image token counts are also estimates); a runtime whose
        tokenizer cannot be reached fails cleanly via NotImplementedError.
        """
        if self.model is None:
            raise RuntimeError("model is not loaded")
        from mlx_vlm.prompt_utils import apply_chat_template
        prompt = params.get("messages", params.get("prompt", ""))
        images = params.get("images") or []
        config = getattr(self.model, "config", None)
        last_error: Exception | None = None
        rendered = prompt if isinstance(prompt, str) else None
        if rendered is None:
            for extra_kwargs in tool_template_kwargs_attempts(params):
                try:
                    rendered = apply_chat_template(
                        self.processor, config, prompt, num_images=len(images), **extra_kwargs)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            if rendered is None:
                raise last_error if last_error else RuntimeError("could not render prompt")
        text = rendered if isinstance(rendered, str) else str(rendered)
        for encoder in (getattr(getattr(self.processor, "tokenizer", None), "encode", None),
                        getattr(self.processor, "encode", None)):
            if callable(encoder):
                try:
                    return len(list(encoder(text)))
                except Exception:  # noqa: BLE001 - try the next encoder
                    continue
        raise NotImplementedError("no reachable tokenizer for count_tokens")

    def stream(self, request_id: str, params: dict):
        if self.model is None:
            raise RuntimeError("model is not loaded")
        from mlx_vlm import stream_generate
        prompt = params.get("messages", params.get("prompt", ""))
        images = params.get("images") or []
        if images and "image" not in self.modalities:
            raise ValueError("このモデルは画像入力に対応していません")
        from mlx_vlm.prompt_utils import apply_chat_template
        config = getattr(self.model, "config", None)
        last_error: Exception | None = None
        tool_support = "none"
        for extra_kwargs in tool_template_kwargs_attempts(params):
            try:
                prompt = apply_chat_template(
                    self.processor, config, prompt, num_images=len(images), **extra_kwargs
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
        kwargs = {"model": self.model, "processor": self.processor, "prompt": prompt,
                  "max_tokens": int(params.get("max_tokens", 512)),
                  "temperature": float(params.get("temperature", 0.7)),
                  "top_p": float(params.get("top_p", 1.0)),
                  "repetition_context_size": int(params.get("repetition_context_size", 20))}
        seed = params.get("seed")
        if isinstance(seed, int) and not isinstance(seed, bool):
            try:
                import mlx.core as mx
                mx.random.seed(seed)
            except Exception:
                pass
        repetition_penalty = float(params.get("repetition_penalty", 1.0))
        if repetition_penalty != 1.0:
            kwargs["repetition_penalty"] = repetition_penalty
        for key in ("presence_penalty", "frequency_penalty"):
            value = float(params.get(key, 0.0))
            if value:
                kwargs[key] = value
        if images:
            kwargs["image"] = images if len(images) > 1 else images[0]
        elif self.prompt_cache_state is not None or self.apc_manager is not None:
            # PromptCacheState reuses the longest token prefix from the previous
            # text request. Never share it with image requests: equal image
            # placeholder tokens do not prove that the underlying pixels match.
            if self.prompt_cache_state is not None:
                kwargs["prompt_cache_state"] = self.prompt_cache_state
                self._pending_prompt_ids = None
                begin = getattr(self.prompt_cache_state, "begin_request", None)
                if callable(begin):
                    begin()
            if self.apc_manager is not None:
                self._tune_apc_guard()
                kwargs["apc_manager"] = self.apc_manager
                kwargs["apc_tenant"] = "mlxbar-local"
        estimate = self._prefill_estimate(prompt)
        if estimate is not None:
            yield {"type": "prefill_estimate", **estimate}
        tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
        if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        completed = False
        apc_before = self._apc_stats_snapshot()
        received_response = False
        generated = 0
        generated_ids: list[int] = []
        # A retry replaces the state object, and with it the probe that explains
        # why this request is slow. Carried across so the reason survives the
        # recovery instead of being reported as an ordinary cold request.
        retry_probe: dict | None = None
        ticker = _ProgressTicker(params)
        try:
            try:
                for response in stream_generate(**kwargs):
                    # Counted before the cancellation check, and deliberately.
                    # The runtime advanced the cache to produce this response, so
                    # the token belongs to the cache whether or not its text ever
                    # reaches the client. Checking first would leave the cache one
                    # token ahead of its own labels, and the settling check would
                    # then -- correctly -- refuse to keep it.
                    _collect_token(generated_ids, response)
                    if request_id in self.cancelled:
                        return
                    received_response = True
                    generated += 1
                    last_response = response
                    text = getattr(response, "text", response if isinstance(response, str) else "")
                    if text:
                        yield {"type": "delta", "text": text, **_live_progress(response)}
                    elif ticker.due():
                        yield {"type": "token_progress", **_live_progress(response)}
                completed = True
            except Exception as exc:
                apc_failed = kwargs.get("apc_manager") is not None and self._is_apc_failure(exc)
                reuse_failed = kwargs.get("prompt_cache_state") is not None and self._is_cache_reuse_failure(exc)
                if not (apc_failed or reuse_failed) or received_response:
                    if apc_failed:
                        self._disable_apc_after_failure(exc)
                    raise
                # Reuse can fail because a runtime or cache format changed, or
                # because the runtime cannot roll a cache back to a shorter
                # shared prefix for this architecture. Before anything was
                # emitted it is safe to retry once with a fresh cache, which
                # skips reuse entirely for this request.
                retry_probe = dict(getattr(self.prompt_cache_state, "last_probe", None) or {})
                retry_probe.update({"action": "cold",
                                    "reason": cache_state.COLD_REUSE_UNSUPPORTED})
                if apc_failed:
                    self._disable_apc_after_failure(exc)
                    kwargs.pop("apc_manager", None)
                    kwargs.pop("apc_tenant", None)
                else:
                    logging.getLogger(__name__).warning(
                        "Prompt cache reuse failed; retrying with a fresh cache: %s", exc)
                    self.prompt_cache_reuse_failures += 1
                self._reset_prompt_cache()
                kwargs["prompt_cache_state"] = self.prompt_cache_state
                last_response = None
                generated_ids = []
                for response in stream_generate(**kwargs):
                    _collect_token(generated_ids, response)
                    if request_id in self.cancelled:
                        return
                    generated += 1
                    last_response = response
                    text = getattr(response, "text", response if isinstance(response, str) else "")
                    if text:
                        yield {"type": "delta", "text": text, **_live_progress(response)}
                    elif ticker.due():
                        yield {"type": "token_progress", **_live_progress(response)}
                completed = True
        finally:
            if not completed and kwargs.get("prompt_cache_state") is not None:
                # stream_generate mutates a reused cache in place, and it only
                # writes the matching token ids back when a generation runs to
                # the end. An interrupted turn therefore leaves the cache one
                # step ahead of its own labels; settling re-pairs them so the
                # work already done survives, and discards only when the pairing
                # cannot be proven.
                self._settle_interrupted_cache(request_id, generated_ids)
        if completed and kwargs.get("prompt_cache_state") is not None:
            self._remember_boundary()
        if (last_response is not None and not isinstance(last_response, str)
                and hasattr(last_response, "prompt_tokens")):
            apc_after = self._apc_stats_snapshot()
            cached_tokens = int(getattr(last_response, "cached_tokens", 0) or 0)
            probe = retry_probe or (getattr(self.prompt_cache_state, "last_probe", None) or {})
            if probe.get("action") == "restore":
                # A restored snapshot is the reuse, whichever tier it came from.
                cache_tier = "disk" if probe.get("restoredFrom") == "disk" else "memory"
            elif int(apc_after.get("disk_hits", 0)) > int(apc_before.get("disk_hits", 0)):
                cache_tier = "disk"
            elif cached_tokens > 0:
                cache_tier = "memory"
            else:
                cache_tier = "cold"
            cold_reason = self._cold_reason(cache_tier, probe)
            self._learn_prefill_rate(prompt, last_response, cached_tokens)
            yield {"type": "usage",
                   "prompt_tokens": int(getattr(last_response, "prompt_tokens", 0) or 0),
                   "completion_tokens": int(getattr(last_response, "generation_tokens", 0) or 0)}
            finish_reason = getattr(last_response, "finish_reason", None)
            if generated >= int(params.get("max_tokens", 512)):
                finish_reason = "length"
            yield {"type": "metrics",
                   "prompt_tokens": int(getattr(last_response, "prompt_tokens", 0) or 0),
                   "cached_tokens": cached_tokens,
                   "cache_tier": cache_tier,
                   "cold_reason": cold_reason,
                   "shared_prefix_tokens": int(probe.get("sharedPrefix", 0) or 0),
                   "held_prefix_tokens": int(probe.get("heldTokens", 0) or 0),
                   "finish_reason": finish_reason if finish_reason in {"stop", "length"} else None,
                   "tool_support": tool_support,
                   "prompt_tps": float(getattr(last_response, "prompt_tps", 0.0) or 0.0),
                   "generation_tps": float(getattr(last_response, "generation_tps", 0.0) or 0.0)}

    def _cold_reason(self, cache_tier: str, probe: dict) -> str | None:
        """Why this request had to prefill from scratch, if it did."""
        if cache_tier != "cold":
            self.last_cold_reason = None
            self._pending_cold_reason = None
            return None
        reason = probe.get("reason") or cache_state.COLD_NO_PREFIX
        pending, self._pending_cold_reason = self._pending_cold_reason, None
        if pending and reason in {cache_state.COLD_NO_PREFIX, cache_state.COLD_FIRST_REQUEST}:
            # The previous turn explains this one better than "no shared prefix"
            # does: the prefix is missing *because* that turn was thrown away.
            reason = pending
        if (reason == cache_state.COLD_REUSE_UNSUPPORTED
                and self.checkpoints is not None
                and self.checkpoints.disabled_reason in cache_state.COLD_REASONS):
            reason = self.checkpoints.disabled_reason
        self.last_cold_reason = reason
        return reason

    def _settle_interrupted_cache(self, request_id: str, generated_ids: list[int]) -> None:
        """Re-pair an interrupted cache with its token ids, or discard it.

        The rewrite is only allowed when the cache can be shown to hold exactly
        the tokens being claimed for it. Where the runtime cold-prefilled into a
        cache of its own, the retained one never moved and the check fails on
        its own, which is why no separate "was it reused" flag is needed.
        """
        state = self.prompt_cache_state
        prompt_ids = self._pending_prompt_ids
        self._pending_prompt_ids = None
        if self.abort_reason(request_id) == cache_state.COLD_MEMORY_PRESSURE:
            # Keeping a cache alive is the opposite of what this interruption
            # asked for: it stopped the generation to give memory back.
            self._pending_cold_reason = cache_state.COLD_MEMORY_PRESSURE
            if self.checkpoints is not None:
                self.checkpoints.forget()
            self._reset_prompt_cache()
            return
        if state is None or prompt_ids is None or getattr(state, "cache", None) is None:
            self._pending_cold_reason = cache_state.COLD_CANCELLED_PREVIOUS
            self._reset_prompt_cache()
            return
        expected = len(prompt_ids) + len(generated_ids)
        observed = cache_state.cached_length(state.cache)
        if observed is None or observed != expected:
            # Either the runtime does not report an offset, or the cache is not
            # where the token ids say it is. Both mean the pairing cannot be
            # proven, and an unprovable pairing is silent corruption.
            self._pending_cold_reason = (cache_state.COLD_TOKEN_IDS_UNAVAILABLE
                                         if observed is None
                                         else cache_state.COLD_CANCELLED_PREVIOUS)
            self._reset_prompt_cache()
            return
        state.token_ids = list(prompt_ids) + list(generated_ids)

    def _remember_boundary(self) -> None:
        """Snapshot the end of a completed turn for architectures that need it.

        The end of a completed turn is the last point every later prompt in a
        linear conversation still shares: the next one is this one plus a reply
        and a new message. Capturing here -- rather than at the end of a prompt
        mid-generation -- means the snapshot is taken from a cache the runtime
        has already labelled, so there is no timing subtlety to get wrong.
        """
        store = self.checkpoints
        state = self.prompt_cache_state
        if store is None or state is None:
            return
        ids = list(getattr(state, "token_ids", None) or [])
        cache = getattr(state, "cache", None)
        if not ids or cache is None:
            return
        observed = cache_state.cached_length(cache)
        if observed is not None and observed != len(ids):
            return
        limit = self._memory_limit_ratio()
        if limit > 0 and memory_pressure_reason(self, limit) is not None:
            # A snapshot is a second copy of the cache. Under pressure the
            # cheapest correct action is to keep the model answering.
            store.forget()
            return
        if not store.remember(ids, cache):
            return
        if self._should_persist(len(ids)):
            reason = store.persist(ids)
            if reason is None:
                self._persisted_tokens = len(ids)

    def _should_persist(self, tokens: int) -> bool:
        """Whether this boundary is worth a write.

        Every write is roughly `perTokenBytes * tokens` of SSD traffic, so
        persisting each turn of a long conversation would cost gigabytes per
        turn for a snapshot the next write supersedes minutes later. Growth
        thresholds keep the number of writes logarithmic in conversation length
        while still leaving something recent to resume from.
        """
        if tokens < 256:
            return False
        previous = self._persisted_tokens
        if previous <= 0:
            return True
        return tokens >= previous * 1.25 or tokens - previous >= 8192

    def _prefill_estimate(self, prompt) -> dict | None:
        """How long a cold prefill of this prompt would take, if it is knowable.

        Both terms are measured from this worker's own completed requests
        rather than assumed: characters per token varies by tokenizer and by
        language, and the prefill rate varies by model, quantisation and
        machine. Until one request has finished there is no estimate, which is
        the honest answer rather than a fabricated one.
        """
        if not isinstance(prompt, str) or not prompt:
            return None
        if self._chars_per_token <= 0 or self._cold_prompt_tps <= 0:
            return None
        tokens = int(len(prompt) / self._chars_per_token)
        if tokens < 1024:
            return None
        seconds = tokens / self._cold_prompt_tps
        return {"estimated_prompt_tokens": tokens, "estimated_seconds": round(seconds, 1)}

    def _learn_prefill_rate(self, prompt, response, cached_tokens: int) -> None:
        prompt_tokens = int(getattr(response, "prompt_tokens", 0) or 0)
        if prompt_tokens > 0 and isinstance(prompt, str) and prompt:
            self._chars_per_token = len(prompt) / prompt_tokens
        rate = float(getattr(response, "prompt_tps", 0.0) or 0.0)
        # Only a request that actually prefilled measures the prefill rate. A
        # cached one reports tens of thousands of tokens per second because it
        # processed almost nothing, and predicting from that would promise a
        # two-second wait before an eight-minute one.
        if cached_tokens <= 0 and rate > 0:
            self._cold_prompt_tps = rate

    def _memory_limit_ratio(self) -> float:
        try:
            return float(os.environ.get("MLXBAR_MEMORY_LIMIT_RATIO", "0") or 0)
        except (TypeError, ValueError):
            return 0.0

    def finalize(self, text: str, params: dict) -> dict:
        tools = params.get("tools") or []
        try:
            from mlx_vlm.tool_parsers import _infer_tool_parser_from_processor, load_tool_module
            from mlx_vlm.server.responses_state import process_tool_calls
            parser_type = _infer_tool_parser_from_processor(self.processor)
            if parser_type:
                result = process_tool_calls(text, load_tool_module(parser_type), tools)
                calls = self._normalize_calls(result.get("calls") or [])
                if calls:
                    return {"text": result.get("remaining_text", ""), "tool_calls": calls}
        except Exception:
            pass
        return self._fallback_tool_calls(text, tools)

    @staticmethod
    def _normalize_calls(calls: list) -> list[dict]:
        result = []
        for index, call in enumerate(calls):
            function = call.get("function", call)
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            arguments = function.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            result.append({"id": call.get("id") or f"call_{uuid.uuid4().hex}", "type": "function",
                           "function": {"name": name.strip(), "arguments": arguments}})
        return result

    def _fallback_tool_calls(self, text: str, tools: list) -> dict:
        return parse_tool_markup(text)


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


def _collect_token(collected: list[int], response) -> None:
    """Record the id of a streamed token, when the runtime reports one.

    The ids are needed to re-label an interrupted cache. Decoding the text back
    into ids is not an option: tokenizers do not guarantee that a decode/encode
    round-trip reproduces the original ids, and a single wrong id would pair the
    cache with a sequence the model never saw.
    """
    token = getattr(response, "token", None)
    if isinstance(token, int) and not isinstance(token, bool):
        collected.append(token)


def _tool_support(params: dict, rendered_kwargs: dict) -> str:
    """Whether the rendered template actually carried the requested tools."""
    if not params.get("tools") or params.get("tool_choice") == "none":
        return "none"
    return "full" if rendered_kwargs.get("tools") else "degraded"


if __name__ == "__main__":
    run(MLXVLMAdapter())
