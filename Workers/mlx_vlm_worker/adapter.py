from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from common.server import BaseAdapter, run
from common.tool_calls import parse_tool_markup, tool_template_kwargs_attempts


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

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["modalities"] = self.modalities
        result["promptCaching"] = self.prompt_cache_state is not None
        result["promptCache"] = self.prompt_cache_stats()
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
            self.apc_namespace = f"mlxbar-vlm-v1-{fingerprint}"
            maximum = int(os.environ.get("MLXBAR_PROMPT_CACHE_MAX_BYTES", str(5 << 30)))
            disk = DiskBlockStore(
                self.apc_root,
                namespace=self.apc_namespace,
                max_bytes=max(0, maximum),
            )
            # PromptCacheState remains the warm-memory tier. A zero-block APC
            # manager adds only persistent disk reuse and avoids duplicating a
            # large Qwen hybrid cache in unified memory.
            self.apc_manager = APCManager(num_blocks=0, disk=disk)
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
                       if item.is_dir() and item.name != self.apc_namespace]
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
        }
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
            return sum(1 for item in self.apc_root.iterdir() if item.is_dir())
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
            self.prompt_cache_state = PromptCacheState()
        except (ImportError, AttributeError):
            # Older runtime slots remain usable without the optimization.
            self.prompt_cache_state = None

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        if not path:
            raise ValueError("model path is required")
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
        self._init_apc(path)
        return self.capabilities()

    def unload(self) -> None:
        self._close_apc()
        self.prompt_cache_state = None
        self.model_path = None
        super().unload()

    def clear_prompt_cache(self) -> None:
        self.prompt_cache_state = None
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
        if model_path is not None:
            self._init_apc(model_path)

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
            if self.apc_manager is not None:
                kwargs["apc_manager"] = self.apc_manager
                kwargs["apc_tenant"] = "mlxbar-local"
        tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
        if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        completed = False
        apc_before = self._apc_stats_snapshot()
        received_response = False
        generated = 0
        try:
            try:
                for response in stream_generate(**kwargs):
                    if request_id in self.cancelled:
                        return
                    received_response = True
                    generated += 1
                    last_response = response
                    text = getattr(response, "text", response if isinstance(response, str) else "")
                    if text:
                        yield {"type": "delta", "text": text}
                completed = True
            except Exception as exc:
                apc_failed = kwargs.get("apc_manager") is not None and self._is_apc_failure(exc)
                if not apc_failed or received_response:
                    if apc_failed:
                        self._disable_apc_after_failure(exc)
                    raise
                # Disk lookup can fail because a runtime or cache format changed.
                # Before anything was emitted it is safe to retry once through
                # the already-proven v1.3.7 memory/cold path.
                self._disable_apc_after_failure(exc)
                self._reset_prompt_cache()
                kwargs.pop("apc_manager", None)
                kwargs.pop("apc_tenant", None)
                kwargs["prompt_cache_state"] = self.prompt_cache_state
                last_response = None
                for response in stream_generate(**kwargs):
                    if request_id in self.cancelled:
                        return
                    generated += 1
                    last_response = response
                    text = getattr(response, "text", response if isinstance(response, str) else "")
                    if text:
                        yield {"type": "delta", "text": text}
                completed = True
        finally:
            if not completed and kwargs.get("prompt_cache_state") is not None:
                # stream_generate mutates a reused cache in place. A cancelled
                # or failed iteration cannot leave token_ids paired with a
                # partially advanced cache.
                self._reset_prompt_cache()
        if (last_response is not None and not isinstance(last_response, str)
                and hasattr(last_response, "prompt_tokens")):
            apc_after = self._apc_stats_snapshot()
            cached_tokens = int(getattr(last_response, "cached_tokens", 0) or 0)
            if int(apc_after.get("disk_hits", 0)) > int(apc_before.get("disk_hits", 0)):
                cache_tier = "disk"
            elif cached_tokens > 0:
                cache_tier = "memory"
            else:
                cache_tier = "cold"
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
                   "finish_reason": finish_reason if finish_reason in {"stop", "length"} else None,
                   "tool_support": tool_support,
                   "prompt_tps": float(getattr(last_response, "prompt_tps", 0.0) or 0.0),
                   "generation_tps": float(getattr(last_response, "generation_tps", 0.0) or 0.0)}

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


def _tool_support(params: dict, rendered_kwargs: dict) -> str:
    """Whether the rendered template actually carried the requested tools."""
    if not params.get("tools") or params.get("tool_choice") == "none":
        return "none"
    return "full" if rendered_kwargs.get("tools") else "degraded"


if __name__ == "__main__":
    run(MLXVLMAdapter())
