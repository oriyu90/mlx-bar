from __future__ import annotations

import json
import os
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

import common.server as worker_server  # noqa: E402
from common.server import BaseAdapter, create_app  # noqa: E402
from common.tool_calls import parse_tool_markup  # noqa: E402
from mlx_lm_worker.adapter import MLXLMAdapter  # noqa: E402
from mlx_vlm_worker.adapter import MLXVLMAdapter  # noqa: E402


class ThreadBoundAdapter(BaseAdapter):
    engine = "thread-test"

    def __init__(self):
        super().__init__()
        self.load_thread: int | None = None

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        self.load_thread = threading.get_ident()
        self.model = object()
        return self.capabilities()

    def stream(self, request_id: str, params: dict):
        if threading.get_ident() != self.load_thread:
            raise RuntimeError("model was used from a different thread")
        yield {"type": "delta", "text": "OK"}


class CacheAwareAdapter(ThreadBoundAdapter):
    def __init__(self):
        super().__init__()
        self.cache_cleared = False

    def clear_prompt_cache(self) -> None:
        self.cache_cleared = True


def test_load_and_generation_use_the_same_dedicated_thread():
    adapter = ThreadBoundAdapter()
    with TestClient(create_app(adapter)) as client:
        loaded = client.post("/rpc", json={
            "protocol_version": 1,
            "method": "load",
            "params": {"path": "/model"},
        })
        assert loaded.status_code == 200

        generated = client.post("/generate", json={
            "request_id": "thread-test",
            "params": {"prompt": "test"},
        })

    assert generated.status_code == 200
    assert '"text": "OK"' in generated.text
    assert "different thread" not in generated.text


def test_worker_exposes_prompt_cache_clear_rpc():
    adapter = CacheAwareAdapter()
    with TestClient(create_app(adapter)) as client:
        response = client.post("/rpc", json={
            "protocol_version": 1, "method": "clear_prompt_cache", "params": {},
        })
    assert response.status_code == 200
    assert adapter.cache_cleared is True


class SlowPrefillAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        time.sleep(0.12)
        yield {"type": "delta", "text": "ready"}


def test_long_prefill_emits_heartbeats_before_first_token():
    previous = worker_server.GENERATION_HEARTBEAT_SECONDS
    worker_server.GENERATION_HEARTBEAT_SECONDS = 0.03
    try:
        adapter = SlowPrefillAdapter()
        with TestClient(create_app(adapter)) as client:
            client.post("/rpc", json={"protocol_version": 1, "method": "load",
                                       "params": {"path": "/model"}})
            generated = client.post("/generate", json={"request_id": "slow-prefill",
                                                        "params": {"prompt": "long prompt",
                                                                   "heartbeat_interval_seconds": 0.03}})
        lines = [line for line in generated.text.splitlines() if line]
        heartbeat_index = next(index for index, line in enumerate(lines) if '"type": "heartbeat"' in line)
        token_index = next(index for index, line in enumerate(lines) if '"text": "ready"' in line)
        assert heartbeat_index < token_index
        assert sum('"type": "heartbeat"' in line for line in lines) >= 2
    finally:
        worker_server.GENERATION_HEARTBEAT_SECONDS = previous


class SlowBufferedToolAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        for text in ("<tool", "_call>", "read", "</tool_call>"):
            time.sleep(0.02)
            yield {"type": "delta", "text": text}


def test_buffered_tool_generation_still_emits_heartbeats():
    previous = worker_server.GENERATION_HEARTBEAT_SECONDS
    worker_server.GENERATION_HEARTBEAT_SECONDS = 0.03
    try:
        adapter = SlowBufferedToolAdapter()
        with TestClient(create_app(adapter)) as client:
            client.post("/rpc", json={"protocol_version": 1, "method": "load",
                                       "params": {"path": "/model"}})
            generated = client.post("/generate", json={"request_id": "slow-tool", "params": {
                "prompt": "use a tool", "tools": [{"type": "function", "function": {"name": "read"}}],
                "tool_choice": "auto", "heartbeat_interval_seconds": 0.03,
            }})
        assert '"type": "heartbeat"' in generated.text
        assert '"phase": "tool_parse"' in generated.text
    finally:
        worker_server.GENERATION_HEARTBEAT_SECONDS = previous


class IncrementalPlainToolAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        for text in ("Hello", " from", " MLXBar"):
            yield {"type": "delta", "text": text}


def test_normal_tool_capable_response_is_streamed_incrementally():
    adapter = IncrementalPlainToolAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "plain-tool", "params": {
            "prompt": "hello", "tools": [{"type": "function", "function": {"name": "read"}}],
            "tool_choice": "auto",
        }})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    assert deltas == ["Hello", " from", " MLXBar"]
    assert events[-1] == {"type": "completed", "finish_reason": "stop"}


class IncrementalReasoningToolAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        yield {"type": "reasoning_start"}
        for text in ("private reasoning", "</thi", "nk>Visible answer. ", "<tool", "_call>",
                     '{"name":"read","arguments":{"path":"README.md"}}', "</tool_call>"):
            yield {"type": "delta", "text": text}

    def finalize(self, text: str, params: dict) -> dict:
        return parse_tool_markup(text)


def test_reasoning_and_tool_markup_are_separated_while_streaming():
    adapter = IncrementalReasoningToolAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "reasoning-tool", "params": {
            "prompt": "use a tool", "tools": [{"type": "function", "function": {"name": "read"}}],
            "tool_choice": "auto",
        }})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    assert "".join(event["text"] for event in events if event.get("type") == "reasoning_delta") == "private reasoning"
    assert "".join(event["text"] for event in events if event.get("type") == "delta") == "Visible answer. "
    calls = [event for event in events if event.get("type") == "tool_calls"]
    assert calls[0]["calls"][0]["function"]["name"] == "read"
    assert not any("<think>" in str(event) or "<tool_call>" in str(event) for event in events)
    assert events[-1] == {"type": "completed", "finish_reason": "tool_calls"}


class BrokenToolMarkupAdapter(IncrementalReasoningToolAdapter):
    def stream(self, request_id: str, params: dict):
        yield {"type": "delta", "text": "<tool_call>not valid</tool_call>"}


def test_detected_but_unparseable_tool_call_returns_explicit_error():
    adapter = BrokenToolMarkupAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "broken-tool", "params": {
            "prompt": "use a tool", "tools": [{"type": "function", "function": {"name": "read"}}],
            "tool_choice": "auto",
        }})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "TOOL_PARSE_FAILED"


def test_mlx_lm_sampling_parameters_reach_sampler_and_logits_processors():
    captured = {}
    mlx_lm = ModuleType("mlx_lm")
    sample_utils = ModuleType("mlx_lm.sample_utils")

    def stream_generate(_model, _processor, **kwargs):
        captured["generate"] = kwargs
        yield SimpleNamespace(text="ok")

    def make_sampler(**kwargs):
        captured["sampler"] = kwargs
        return "sampler"

    def make_logits_processors(**kwargs):
        captured["processors"] = kwargs
        return ["processor"]

    mlx_lm.stream_generate = stream_generate
    sample_utils.make_sampler = make_sampler
    sample_utils.make_logits_processors = make_logits_processors
    adapter = MLXLMAdapter()
    adapter.model = object()
    adapter.processor = SimpleNamespace(apply_chat_template=lambda *_args, **_kwargs: "prompt")
    with patch.dict(sys.modules, {"mlx_lm": mlx_lm, "mlx_lm.sample_utils": sample_utils}):
        events = list(adapter.stream("request", {"prompt": "hello", "temperature": 0.3,
            "top_p": 0.82, "repetition_penalty": 1.14, "repetition_context_size": 80,
            "presence_penalty": 0.2, "frequency_penalty": 0.4}))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured["sampler"] == {"temp": 0.3, "top_p": 0.82}
    assert captured["processors"]["repetition_penalty"] == 1.14
    assert captured["processors"]["repetition_context_size"] == 80
    assert captured["generate"]["sampler"] == "sampler"
    assert captured["generate"]["logits_processors"] == ["processor"]


def test_mlx_lm_chat_template_kwargs_are_preserved_with_tools():
    captured = []
    mlx_lm = ModuleType("mlx_lm")

    def stream_generate(_model, _processor, **_kwargs):
        yield SimpleNamespace(text="ok")

    def apply_chat_template(*_args, **kwargs):
        captured.append(kwargs)
        if "tool_choice" in kwargs:
            raise TypeError("tool_choice unsupported")
        return "prompt"

    mlx_lm.stream_generate = stream_generate
    adapter = MLXLMAdapter()
    adapter.model = object()
    adapter.processor = SimpleNamespace(apply_chat_template=apply_chat_template)
    with patch.dict(sys.modules, {"mlx_lm": mlx_lm}):
        events = list(adapter.stream("request", {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "read"}}],
            "tool_choice": "auto",
            "chat_template_kwargs": {"enable_thinking": False},
        }))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured[-1]["enable_thinking"] is False
    assert "tool_choice" not in captured[-1]


def test_mlx_vlm_retries_without_tool_choice_when_template_rejects_it():
    captured = {}
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(text="ok")

    def apply_chat_template(_processor, _config, _prompt, **kwargs):
        if "tool_choice" in kwargs:
            raise TypeError("apply_chat_template() got an unexpected keyword argument 'tool_choice'")
        return "templated prompt"

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = apply_chat_template
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {
            "messages": [{"role": "user", "content": "hello"}], "tool_choice": "auto",
        }))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured["prompt"] == "templated prompt"


def test_mlx_vlm_chat_template_kwargs_reach_template():
    captured = {}
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**_kwargs):
        yield SimpleNamespace(text="ok")

    def apply_chat_template(_processor, _config, _prompt, **kwargs):
        captured.update(kwargs)
        return "templated prompt"

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = apply_chat_template
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {
            "messages": [{"role": "user", "content": "hello"}],
            "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "max"},
        }))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured["enable_thinking"] is False
    assert captured["reasoning_effort"] == "max"


def test_mlx_vlm_retries_openai_high_as_qwen_xhigh():
    captured = []
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**_kwargs):
        yield SimpleNamespace(text="ok")

    def apply_chat_template(_processor, _config, _prompt, **kwargs):
        captured.append(kwargs.get("reasoning_effort"))
        if kwargs.get("reasoning_effort") == "high":
            raise ValueError("Qwen supports xhigh, medium, and low")
        return "prompt"

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = apply_chat_template
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {
            "messages": [{"role": "user", "content": "hello"}],
            "chat_template_kwargs": {"reasoning_effort": "high"},
        }))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured == ["high", "xhigh"]


def test_mlx_vlm_retries_without_tools_when_template_rejects_them_entirely():
    """A template with no tool-calling support at all can fail deep inside Jinja2
    rendering (e.g. UndefinedError), not with a clean TypeError. That must still
    fall back to a plain template instead of failing the whole generation."""
    captured = {}
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(text="ok")

    def apply_chat_template(_processor, _config, _prompt, **kwargs):
        if "tools" in kwargs:
            raise Exception("jinja2.exceptions.UndefinedError: 'tools' is undefined")
        return "templated prompt"

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = apply_chat_template
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
        }))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured["prompt"] == "templated prompt"


def test_mlx_vlm_template_failure_is_not_silently_swallowed():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**_kwargs):
        raise AssertionError("stream_generate must not run with an unresolved prompt")
        yield  # pragma: no cover

    def apply_chat_template(*_args, **_kwargs):
        raise RuntimeError("laguna template is incompatible with this mlx-vlm version")

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = apply_chat_template
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        try:
            list(adapter.stream("request", {"messages": [{"role": "user", "content": "hello"}]}))
        except RuntimeError as exc:
            assert "incompatible" in str(exc)
        else:
            raise AssertionError("expected the template failure to propagate")


def test_mlx_vlm_sampling_parameters_reach_stream_generate():
    captured = {}
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(text="ok")

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {"prompt": "hello", "temperature": 0.4,
            "top_p": 0.76, "repetition_penalty": 1.09, "repetition_context_size": 48,
            "presence_penalty": 0.1, "frequency_penalty": 0.2}))
    assert events == [{"type": "delta", "text": "ok"}]
    assert captured["temperature"] == 0.4
    assert captured["top_p"] == 0.76
    assert captured["repetition_penalty"] == 1.09
    assert captured["repetition_context_size"] == 48
    assert captured["presence_penalty"] == 0.1
    assert captured["frequency_penalty"] == 0.2
    assert captured["prompt_cache_state"] is adapter.prompt_cache_state


def test_mlx_vlm_prompt_cache_is_not_shared_with_image_requests():
    captured = {}
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**kwargs):
        captured.update(kwargs)
        yield SimpleNamespace(text="ok")

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text", "image"]
    adapter.prompt_cache_state = object()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        list(adapter.stream("request", {"prompt": "hello", "images": ["/private/image.png"]}))
    assert captured["image"] == "/private/image.png"
    assert "prompt_cache_state" not in captured


def test_mlx_vlm_reports_runtime_usage_and_cache_metrics():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    def stream_generate(**_kwargs):
        yield SimpleNamespace(text="ok", prompt_tokens=4096, generation_tokens=7,
                              cached_tokens=3900, prompt_tps=850.5, generation_tps=12.25)

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {"prompt": "hello"}))
    assert events[-2] == {"type": "usage", "prompt_tokens": 4096, "completion_tokens": 7}
    assert events[-1] == {"type": "metrics", "prompt_tokens": 4096,
                          "cached_tokens": 3900, "prompt_tps": 850.5,
                          "generation_tps": 12.25, "cache_tier": "memory"}


def test_mlx_vlm_cancelled_stream_discards_mutated_prompt_cache():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    generate_module = ModuleType("mlx_vlm.generate")

    class PromptCacheState:
        pass

    def stream_generate(**_kwargs):
        yield SimpleNamespace(text="first")
        yield SimpleNamespace(text="second")

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    generate_module.PromptCacheState = PromptCacheState
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    original = PromptCacheState()
    adapter.prompt_cache_state = original
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils,
                                  "mlx_vlm.generate": generate_module}):
        iterator = adapter.stream("request", {"prompt": "hello"})
        assert next(iterator) == {"type": "delta", "text": "first"}
        iterator.close()
    assert isinstance(adapter.prompt_cache_state, PromptCacheState)
    assert adapter.prompt_cache_state is not original


def test_mlx_vlm_disk_apc_is_layered_below_prompt_cache_state():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    class Manager:
        def __init__(self):
            self.disk_hits = 0

        def stats_snapshot(self):
            return {"disk_hits": self.disk_hits}

    manager = Manager()
    captured = {}

    def stream_generate(**kwargs):
        captured.update(kwargs)
        manager.disk_hits += 1
        yield SimpleNamespace(text="ok", prompt_tokens=100, generation_tokens=1,
                              cached_tokens=96, prompt_tps=1000, generation_tps=10)

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    adapter.apc_manager = manager
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {"prompt": "hello"}))
    assert captured["prompt_cache_state"] is adapter.prompt_cache_state
    assert captured["apc_manager"] is manager
    assert captured["apc_tenant"] == "mlxbar-local"
    assert events[-1]["cache_tier"] == "disk"


def test_mlx_vlm_apc_failure_retries_once_without_disk_cache():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    generate_module = ModuleType("mlx_vlm.generate")
    calls = []

    class PromptCacheState:
        pass

    class Manager:
        closed = False

        def stats_snapshot(self):
            return {"disk_hits": 0}

        def close(self):
            self.closed = True

    manager = Manager()

    def stream_generate(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("apc_manager") is manager:
            raise RuntimeError("corrupt APC safetensors header")
        yield SimpleNamespace(text="recovered", prompt_tokens=20, generation_tokens=1,
                              cached_tokens=0, prompt_tps=10, generation_tps=5)

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    generate_module.PromptCacheState = PromptCacheState
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = PromptCacheState()
    adapter.apc_manager = manager
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils,
                                  "mlx_vlm.generate": generate_module}):
        events = list(adapter.stream("request", {"prompt": "hello"}))
    assert len(calls) == 2
    assert calls[0]["apc_manager"] is manager
    assert "apc_manager" not in calls[1]
    assert manager.closed is True
    assert adapter.apc_manager is None
    assert adapter.apc_disabled_reason == "runtime_failed:RuntimeError"
    assert events[0] == {"type": "delta", "text": "recovered"}
    assert events[-1]["cache_tier"] == "cold"


def test_mlx_vlm_unrelated_failure_is_not_retried_or_mislabelled_as_apc():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    calls = []

    class Manager:
        def stats_snapshot(self):
            return {"disk_hits": 0}

        def close(self):
            raise AssertionError("unrelated failures must not disable APC")

    def stream_generate(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("model kernel rejected an invalid shape")
        yield  # pragma: no cover

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    adapter.apc_manager = Manager()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        try:
            list(adapter.stream("request", {"prompt": "hello"}))
        except RuntimeError as exc:
            assert "invalid shape" in str(exc)
        else:
            raise AssertionError("expected model failure")
    assert len(calls) == 1
    assert adapter.apc_manager is not None


def test_mlx_vlm_broken_apc_stats_do_not_break_generation():
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")

    class Manager:
        def stats_snapshot(self):
            raise RuntimeError("diagnostics unavailable")

    def stream_generate(**_kwargs):
        yield SimpleNamespace(text="ok", prompt_tokens=10, generation_tokens=1,
                              cached_tokens=0, prompt_tps=100, generation_tps=10)

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "prompt"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    adapter.apc_manager = Manager()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {"prompt": "hello"}))
    assert events[0] == {"type": "delta", "text": "ok"}
    assert events[-1]["cache_tier"] == "cold"


def test_mlx_vlm_model_fingerprint_changes_with_runtime_and_weight_metadata(tmp_path):
    (tmp_path / "config.json").write_text('{"model_type":"qwen"}')
    weight = tmp_path / "model.safetensors"
    weight.write_bytes(b"one")
    first = MLXVLMAdapter._model_fingerprint(tmp_path, "0.6.15")
    second = MLXVLMAdapter._model_fingerprint(tmp_path, "0.6.16")
    weight.write_bytes(b"different")
    third = MLXVLMAdapter._model_fingerprint(tmp_path, "0.6.15")
    assert len(first) == 64
    assert first != second
    assert first != third


def test_mlx_vlm_initializes_disk_only_apc_with_bounded_private_store(tmp_path):
    apc_module = ModuleType("mlx_vlm.apc")
    captured = {}

    class DiskBlockStore:
        def __init__(self, root, namespace, max_bytes):
            captured.update(root=Path(root), namespace=namespace, max_bytes=max_bytes)
            self.dir = Path(root) / namespace
            self.dir.mkdir(parents=True)

    class APCManager:
        def __init__(self, num_blocks, disk):
            captured.update(num_blocks=num_blocks, disk=disk)
            self.disk = disk

        def stats_snapshot(self):
            return {"disk_bytes": 0, "disk_hits": 0}

        def close(self):
            captured["closed"] = True

    apc_module.DiskBlockStore = DiskBlockStore
    apc_module.APCManager = APCManager
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    cache = tmp_path / "cache"
    adapter = MLXVLMAdapter()
    with patch.dict(sys.modules, {"mlx_vlm.apc": apc_module}), patch.dict(os.environ, {
        "MLXBAR_PROMPT_CACHE_DISK_ENABLED": "1",
        "MLXBAR_PROMPT_CACHE_ROOT": str(cache),
        "MLXBAR_PROMPT_CACHE_MAX_BYTES": "123456",
    }), patch("mlx_vlm_worker.adapter.importlib.metadata.version", return_value="0.6.15"):
        adapter._init_apc(str(model))
    assert captured["num_blocks"] == 0
    assert captured["max_bytes"] == 123456
    assert captured["namespace"].startswith("mlxbar-vlm-v1-")
    assert captured["disk"].dir.stat().st_mode & 0o777 == 0o700
    adapter._close_apc()
    assert captured["closed"] is True
