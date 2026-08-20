from __future__ import annotations

import json
import uuid

from common.server import BaseAdapter, run
from common.tool_calls import parse_tool_markup, tool_template_kwargs_attempts


class MLXLMAdapter(BaseAdapter):
    engine = "mlx-lm"

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        if not path:
            raise ValueError("model path is required")
        from mlx_lm import load
        try:
            self.model, self.processor = load(path, tokenizer_config={"trust_remote_code": trust_remote_code})
        except TypeError:
            self.model, self.processor = load(path)
        return self.capabilities()

    def stream(self, request_id: str, params: dict):
        if self.model is None:
            raise RuntimeError("model is not loaded")
        from mlx_lm import stream_generate
        prompt = params.get("messages", params.get("prompt", ""))
        if isinstance(prompt, list):
            last_error: Exception | None = None
            for extra_kwargs in tool_template_kwargs_attempts(params):
                try:
                    prompt = self.processor.apply_chat_template(
                        prompt, tokenize=False, add_generation_prompt=True, **extra_kwargs
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
        kwargs = {"prompt": prompt,
                  "max_tokens": int(params.get("max_tokens", 512))}
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
        tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
        if tool_mode and isinstance(prompt, str) and prompt.rstrip().endswith("<think>"):
            yield {"type": "reasoning_start"}
        last_response = None
        for response in stream_generate(self.model, self.processor, **kwargs):
            last_response = response
            text = getattr(response, "text", response if isinstance(response, str) else "")
            if text:
                yield {"type": "delta", "text": text}
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


if __name__ == "__main__":
    run(MLXLMAdapter())
