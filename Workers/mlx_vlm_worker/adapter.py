from __future__ import annotations

import json
import uuid
from pathlib import Path

from common.server import BaseAdapter, run
from common.tool_calls import parse_tool_markup


class MLXVLMAdapter(BaseAdapter):
    engine = "mlx-vlm"

    def __init__(self):
        super().__init__()
        self.modalities = ["text", "image"]

    def capabilities(self) -> dict:
        result = super().capabilities()
        result["modalities"] = self.modalities
        return result

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
        return self.capabilities()

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
        template_kwargs = {}
        if params.get("tools"):
            template_kwargs["tools"] = params["tools"]
        if params.get("tool_choice") is not None:
            template_kwargs["tool_choice"] = params["tool_choice"]
        try:
            prompt = apply_chat_template(
                self.processor, config, prompt, num_images=len(images), **template_kwargs
            )
        except TypeError:
            template_kwargs.pop("tool_choice", None)
            prompt = apply_chat_template(
                self.processor, config, prompt, num_images=len(images), **template_kwargs
            )
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
        for response in stream_generate(**kwargs):
            text = getattr(response, "text", response if isinstance(response, str) else "")
            if text:
                yield {"type": "delta", "text": text}

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
