from __future__ import annotations

import importlib.metadata
import os

from common.server import BaseAdapter, run
from common.tool_calls import parse_tool_markup, tool_template_kwargs_attempts

from .prompt_cache import PromptCacheStore


class MLXLMAdapter(BaseAdapter):
    engine = "mlx-lm"

    def __init__(self):
        super().__init__()
        self.prompt_cache: PromptCacheStore | None = None
        self.model_path: str | None = None

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["promptCaching"] = self.prompt_cache is not None
        result["promptCache"] = self.prompt_cache_stats()
        return result

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
        self.prompt_cache = PromptCacheStore(
            path, runtime_version,
            root=os.environ.get("MLXBAR_PROMPT_CACHE_ROOT"),
            disk_enabled=self._truthy(os.environ.get("MLXBAR_PROMPT_CACHE_DISK_ENABLED", "1")),
            max_bytes=max_bytes,
            keep_generations=min(10, max(1, keep)),
        )

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        if not path:
            raise ValueError("model path is required")
        from mlx_lm import load
        try:
            self.model, self.processor = load(path, tokenizer_config={"trust_remote_code": trust_remote_code})
        except TypeError:
            self.model, self.processor = load(path)
        self.model_path = path
        self.apply_memory_limits()
        self._init_prompt_cache(path)
        return self.capabilities()

    def unload(self) -> None:
        self.prompt_cache = None
        self.model_path = None
        super().unload()

    def clear_prompt_cache(self) -> None:
        if self.prompt_cache is not None:
            self.prompt_cache.clear_memory()
        super().clear_prompt_cache()

    def clear_disk_prompt_cache(self) -> None:
        if self.prompt_cache is not None:
            self.prompt_cache.clear_disk()

    def prompt_cache_stats(self) -> dict:
        if self.prompt_cache is None:
            return {"enabled": False, "engine": self.engine}
        return self.prompt_cache.stats()

    def _encode(self, text: str) -> list[int]:
        """Tokenize exactly as `stream_generate` would for the same string."""
        tokenizer = self.processor
        bos = getattr(tokenizer, "bos_token", None)
        add_special_tokens = bos is None or not text.startswith(bos)
        return list(tokenizer.encode(text, add_special_tokens=add_special_tokens))

    def stream(self, request_id: str, params: dict):
        if self.model is None:
            raise RuntimeError("model is not loaded")
        from mlx_lm import stream_generate
        prompt = params.get("messages", params.get("prompt", ""))
        tool_support = "none"
        if isinstance(prompt, list):
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
        if isinstance(prompt, str) and self.prompt_cache is not None:
            try:
                prompt_tokens = self._encode(prompt)
            except Exception:
                prompt_tokens = []
        if prompt_tokens:
            cache, remaining, cache_tier = self.prompt_cache.fetch(self.model, prompt_tokens)
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

        tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
        if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        generated: list[int] = []
        completed = False
        finish_reason = None
        try:
            for response in stream_generate(self.model, self.processor, **kwargs):
                if request_id in self.cancelled:
                    return
                last_response = response
                token = getattr(response, "token", None)
                if isinstance(token, int):
                    generated.append(token)
                finish_reason = getattr(response, "finish_reason", None) or finish_reason
                text = getattr(response, "text", response if isinstance(response, str) else "")
                if text:
                    yield {"type": "delta", "text": text}
            completed = True
        finally:
            # A completed generation is the only safe thing to remember: an
            # aborted one has a cache that no longer matches its token list.
            # The warm tier hands out copies, so an abandoned cache simply goes
            # away rather than corrupting the stored one.
            if completed and cache is not None and prompt_tokens and self.prompt_cache is not None:
                self.prompt_cache.store(self.model, prompt_tokens + generated, cache,
                                        prompt_length=len(prompt_tokens))

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
            yield {"type": "metrics",
                   "prompt_tokens": total_prompt,
                   "cached_tokens": cached,
                   "cache_tier": cache_tier if cached else "cold",
                   "finish_reason": finish_reason,
                   "tool_support": tool_support,
                   "prompt_tps": float(getattr(last_response, "prompt_tps", 0.0) or 0.0),
                   "generation_tps": float(getattr(last_response, "generation_tps", 0.0) or 0.0)}
        elif finish_reason:
            yield {"type": "metrics", "finish_reason": finish_reason}

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


def _tool_support(params: dict, rendered_kwargs: dict) -> str:
    """Whether the rendered template actually carried the requested tools."""
    if not params.get("tools") or params.get("tool_choice") == "none":
        return "none"
    return "full" if rendered_kwargs.get("tools") else "degraded"


if __name__ == "__main__":
    run(MLXLMAdapter())
