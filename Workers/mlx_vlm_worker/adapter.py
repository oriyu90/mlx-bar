from __future__ import annotations

import json
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

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["modalities"] = self.modalities
        result["promptCaching"] = self.prompt_cache_state is not None
        return result

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
        self._reset_prompt_cache()
        return self.capabilities()

    def unload(self) -> None:
        self.prompt_cache_state = None
        super().unload()

    def clear_prompt_cache(self) -> None:
        self.prompt_cache_state = None
        super().clear_prompt_cache()
        self._reset_prompt_cache()

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
        for extra_kwargs in tool_template_kwargs_attempts(params):
            try:
                prompt = apply_chat_template(
                    self.processor, config, prompt, num_images=len(images), **extra_kwargs
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        kwargs = {"model": self.model, "processor": self.processor, "prompt": prompt,
                  "max_tokens": int(params.get("max_tokens", 512)),
                  "temperature": float(params.get("temperature", 0.7)),
                  "top_p": float(params.get("top_p", 1.0)),
                  "repetition_context_size": int(params.get("repetition_context_size", 20))}
        repetition_penalty = float(params.get("repetition_penalty", 1.0))
        if repetition_penalty != 1.0:
            kwargs["repetition_penalty"] = repetition_penalty
        for key in ("presence_penalty", "frequency_penalty"):
            value = float(params.get(key, 0.0))
            if value:
                kwargs[key] = value
        if images:
            kwargs["image"] = images if len(images) > 1 else images[0]
        elif self.prompt_cache_state is not None:
            # PromptCacheState reuses the longest token prefix from the previous
            # text request. Never share it with image requests: equal image
            # placeholder tokens do not prove that the underlying pixels match.
            kwargs["prompt_cache_state"] = self.prompt_cache_state
        tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
        if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        completed = False
        try:
            for response in stream_generate(**kwargs):
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
            yield {"type": "usage",
                   "prompt_tokens": int(getattr(last_response, "prompt_tokens", 0) or 0),
                   "completion_tokens": int(getattr(last_response, "generation_tokens", 0) or 0)}
            yield {"type": "metrics",
                   "prompt_tokens": int(getattr(last_response, "prompt_tokens", 0) or 0),
                   "cached_tokens": int(getattr(last_response, "cached_tokens", 0) or 0),
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


if __name__ == "__main__":
    run(MLXVLMAdapter())
