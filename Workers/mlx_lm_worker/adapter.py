from __future__ import annotations

import json
import uuid

from common.server import BaseAdapter, run
from common.tool_calls import parse_tool_markup


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
            template_kwargs = {"tokenize": False, "add_generation_prompt": True}
            if params.get("tools"):
                template_kwargs["tools"] = params["tools"]
            if params.get("tool_choice") is not None:
                template_kwargs["tool_choice"] = params["tool_choice"]
            try:
                prompt = self.processor.apply_chat_template(prompt, **template_kwargs)
            except TypeError:
                template_kwargs.pop("tool_choice", None)
                prompt = self.processor.apply_chat_template(prompt, **template_kwargs)
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
        for response in stream_generate(self.model, self.processor, **kwargs):
            text = getattr(response, "text", response if isinstance(response, str) else "")
            if text:
                yield {"type": "delta", "text": text}

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
