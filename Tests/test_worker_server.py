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
            # Deliberately shorter than the 0.03 s heartbeat the test configures.
            # A longer gap lets the prefill heartbeat fire in between, and that
            # resets `last_visible_event`, which is what the tool_parse
            # heartbeat measures against -- making the assertion a race.
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
    # Dropping `tools` keeps the generation alive but leaves the model unable
    # to call anything, so it must be reported rather than look like a model
    # that simply chose not to.
    assert events == [{"type": "tool_support", "state": "degraded"},
                      {"type": "delta", "text": "ok"}]
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
                          "generation_tps": 12.25, "cache_tier": "memory",
                          "finish_reason": None, "tool_support": "none"}


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
    assert events[0]["type"] == "delta" and events[0]["text"] == "recovered"
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
    assert events[0]["type"] == "delta" and events[0]["text"] == "ok"
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


# --------------------------------------------------------------------------
# v1.5.0 regressions
# --------------------------------------------------------------------------


class AbandonedGenerationAdapter(ThreadBoundAdapter):
    """Records whether its generator was ever closed, and how far it ran."""

    def __init__(self, total: int = 24):
        super().__init__()
        self.total = total
        self.produced = 0
        self.closed_on: str | None = None

    def stream(self, request_id: str, params: dict):
        try:
            for index in range(self.total):
                self.produced += 1
                yield {"type": "delta", "text": f"t{index} "}
        finally:
            self.closed_on = threading.current_thread().name


def test_disconnected_stream_closes_the_adapter_generator_on_the_mlx_thread():
    """A client that goes away must not leave the generator un-finalised.

    Without an explicit close the adapter's `finally` only ran whenever the
    garbage collector got around to it -- on an arbitrary thread -- so the
    prompt-cache discard that protects a cancelled generation never happened
    in time, and the next request could reuse a half-advanced cache.
    """
    adapter = AbandonedGenerationAdapter()
    with TestClient(create_app(adapter)) as client:
        with client.stream("POST", "/generate", json={
            "protocol_version": 1, "request_id": "abandoned",
            "method": "generate", "params": {"prompt": "hi"},
        }) as response:
            for count, line in enumerate(response.iter_lines()):
                if count >= 3:
                    break
    deadline = time.monotonic() + 5
    while adapter.closed_on is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert adapter.closed_on is not None, "generator was never closed"
    # Closing on the MLX thread matters as much as closing at all: the adapter
    # generator's teardown touches MLX state, which is thread-bound.
    assert adapter.closed_on.startswith(adapter.engine), adapter.closed_on


class StopSequenceAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        for text in ("Answer: 42", " <<E", "ND>> trailing noise"):
            yield {"type": "delta", "text": text}


def test_stop_sequence_truncates_output_even_when_split_across_deltas():
    adapter = StopSequenceAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "stop", "params": {
            "prompt": "hi", "stop": ["<<END>>"]}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    text = "".join(event["text"] for event in events if event.get("type") == "delta")
    assert text == "Answer: 42 "
    assert events[-1] == {"type": "completed", "finish_reason": "stop"}


class LengthLimitedAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        yield {"type": "delta", "text": "cut off here"}
        yield {"type": "metrics", "finish_reason": "length"}


def test_length_finish_reason_reaches_the_client():
    """A truncated reply must not be reported as a completed one."""
    adapter = LengthLimitedAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "len", "params": {"prompt": "hi"}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    assert events[-1] == {"type": "completed", "finish_reason": "length"}


class MemoryHogAdapter(ThreadBoundAdapter):
    def memory_stats(self) -> dict:
        return {"active_bytes": 95, "cache_bytes": 0, "peak_bytes": 95,
                "physical_memory_bytes": 100, "available_bytes": 2,
                "pressure_level": 1, "process_rss_bytes": 95}

    def stream(self, request_id: str, params: dict):
        for index in range(200):
            time.sleep(0.01)
            yield {"type": "delta", "text": f"t{index}"}


def test_generation_is_stopped_when_memory_crosses_the_limit_mid_stream():
    """The pre-flight check cannot see a KV cache that grows while generating."""
    previous = worker_server.MEMORY_CHECK_INTERVAL_SECONDS
    worker_server.MEMORY_CHECK_INTERVAL_SECONDS = 0.0
    try:
        adapter = MemoryHogAdapter()
        with TestClient(create_app(adapter)) as client:
            generated = client.post("/generate", json={"request_id": "mem", "params": {
                "prompt": "hi", "memory_limit_ratio": 0.9}})
    finally:
        worker_server.MEMORY_CHECK_INTERVAL_SECONDS = previous
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "MEMORY_PRESSURE"
    assert events[-1]["retryable"] is True


def test_memory_pressure_reason_uses_free_memory_and_the_os_verdict():
    from common.server import memory_pressure_reason

    class Stub(BaseAdapter):
        def __init__(self, stats):
            super().__init__()
            self.stats = stats

        def memory_stats(self):
            return self.stats

    healthy = {"active_bytes": 10, "cache_bytes": 0, "physical_memory_bytes": 100,
               "available_bytes": 50, "pressure_level": 1, "process_rss_bytes": 10}
    assert memory_pressure_reason(Stub(healthy), 0.9) is None
    # MLX itself is idle, but the machine as a whole has nothing left.
    assert memory_pressure_reason(Stub({**healthy, "available_bytes": 2}), 0.9)
    # macOS says it is in trouble, whatever the ratios say.
    assert memory_pressure_reason(Stub({**healthy, "pressure_level": 4}), 0.9)
    # Resident size counts even when MLX's own counters look small.
    assert memory_pressure_reason(Stub({**healthy, "process_rss_bytes": 95}), 0.9)


def test_every_runtime_tool_marker_is_withheld_from_visible_output():
    """Detection must cover the same dialects the runtime parsers accept."""
    from common.tool_calls import IncrementalToolStream

    for marker in ("<tool_call>", "<|tool_call_start|>", "<minimax:tool_call>",
                   "<atem:function_calls>", "<longcat_tool_call>", "<start_function_call>"):
        stream = IncrementalToolStream()
        visible = stream.feed("here you go " + marker + '{"name": "read"}')
        assert stream.tool_detected, marker
        assert "".join(event["text"] for event in visible) == "here you go ", marker


def test_disconnect_over_a_real_socket_stops_generation():
    """The end-to-end shape: uvicorn over a UDS, as the coordinator speaks to it.

    TestClient buffers the whole response before the caller can stop reading,
    so only a real server and a real disconnect can show that the worker stops
    producing tokens instead of running on for a client that has gone.
    """
    import asyncio
    import tempfile

    import httpx
    import uvicorn

    state = {"produced": 0}

    class SlowAdapter(BaseAdapter):
        engine = "slow-test"

        def stream(self, request_id: str, params: dict):
            for index in range(40):
                time.sleep(0.05)
                state["produced"] = index + 1
                yield {"type": "delta", "text": f"t{index} "}

    socket_path = str(Path(tempfile.mkdtemp()) / "worker.sock")
    server = uvicorn.Server(uvicorn.Config(create_app(SlowAdapter()), uds=socket_path,
                                           log_level="error", access_log=False))

    async def scenario():
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker",
                                     timeout=httpx.Timeout(connect=5, read=None,
                                                           write=5, pool=5)) as client:
            async with client.stream("POST", "/generate", json={
                "protocol_version": 1, "request_id": "disconnect",
                "method": "generate", "params": {"heartbeat_interval_seconds": 5},
            }) as response:
                seen = 0
                async for line in response.aiter_lines():
                    if line:
                        seen += 1
                    if seen >= 4:
                        break
        at_disconnect = state["produced"]
        await asyncio.sleep(1.0)
        server.should_exit = True
        await serving
        return at_disconnect, state["produced"]

    at_disconnect, after = asyncio.run(scenario())
    # One token may still be in flight on the MLX thread when the close lands.
    assert after <= at_disconnect + 1, (at_disconnect, after)
    assert after < 40


def test_memory_check_uses_current_resident_size_not_the_high_water_mark():
    """A transient spike must not lock the worker out for the rest of its life.

    `ru_maxrss` never falls, so using it as "in use" meant one huge prefill
    kept every later request above the limit until the worker restarted.
    """
    from common.server import host_memory

    stats = BaseAdapter().memory_stats()
    # Current size drives the decision; the high-water mark is reported
    # separately as a diagnostic and must never stand in for it.
    assert stats["process_rss_bytes"] > 0
    assert stats["process_rss_bytes"] == host_memory()["process_rss_bytes"]
    assert "peak_rss_bytes" in stats

    import resource
    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    import mmap
    block = mmap.mmap(-1, 400_000_000)
    block.write(b"x" * 400_000_000)
    block.close()
    worker_server._HOST_MEMORY_CACHE.clear()
    after = BaseAdapter().memory_stats()
    # The peak rose and stays risen; the reading the limit uses came back down.
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > peak_before
    assert after["process_rss_bytes"] < after["peak_rss_bytes"]


class StopThenToolAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        yield {"type": "delta", "text": "visible part <<END>> hidden "}
        yield {"type": "delta", "text": '<tool_call>{"name": "rm", "arguments": {}}</tool_call>'}


def test_text_after_a_stop_sequence_cannot_produce_a_tool_call():
    """The client never saw it, so it must not act on it either."""
    adapter = StopThenToolAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "stop-tool", "params": {
            "prompt": "hi", "stop": ["<<END>>"],
            "tools": [{"type": "function", "function": {"name": "rm"}}],
            "tool_choice": "auto"}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    text = "".join(event["text"] for event in events if event.get("type") == "delta")
    assert text == "visible part "
    assert not [event for event in events if event.get("type") == "tool_calls"]
    assert events[-1] == {"type": "completed", "finish_reason": "stop"}


def test_prompt_cache_rollback_failure_retries_instead_of_failing_the_request():
    """A runtime that cannot roll its cache back must not lose the answer.

    mlx-vlm rolls a retained cache back to the shared prefix by calling
    `trim()` on every entry, guarded by a retention check rather than by
    `is_trimmable()`. Hybrid Qwen3.5/3.8 layers use a cache type with no
    `trim` at all, so the reuse path raises
    `'ArraysCache' object has no attribute 'trim'` and the whole generation
    failed with GENERATION_FAILED. Reuse is an optimisation; its failure has
    to degrade to a cold prefill.
    """
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    attempts = {"count": 0}
    # Compile the raiser against the runtime's own path so the exception carries
    # a traceback frame inside `mlx_vlm/generate/dispatch.py` -- which is how the
    # adapter tells reuse failures apart from genuine model errors.
    code = compile("def raise_from_dispatch():\n"
                   "    raise AttributeError(\"'ArraysCache' object has no attribute 'trim'\")\n",
                   "/site-packages/mlx_vlm/generate/dispatch.py", "exec")
    namespace: dict = {}
    exec(code, namespace)

    def stream_generate(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            assert kwargs.get("prompt_cache_state") is not None
            namespace["raise_from_dispatch"]()
        yield SimpleNamespace(text="recovered", prompt_tokens=10, generation_tokens=1,
                              cached_tokens=0, prompt_tps=1.0, generation_tps=1.0,
                              finish_reason="stop")

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "templated"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter._reset_prompt_cache = lambda: setattr(adapter, "prompt_cache_state", object())
    adapter.prompt_cache_state = object()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        events = list(adapter.stream("request", {"prompt": "hello"}))
    assert attempts["count"] == 2, "the failed attempt was not retried"
    deltas = [event for event in events if event.get("type") == "delta"]
    assert [event["text"] for event in deltas] == ["recovered"]
    assert adapter.prompt_cache_reuse_failures == 1


def test_reuse_failure_detection_looks_at_the_failing_call_not_the_module():
    """`stream_generate` lives in the same module as the rollback.

    An earlier version of this check matched any traceback frame inside
    `mlx_vlm/generate/dispatch.py`. Because `stream_generate` is defined
    there, *every* generation error matched, so genuine model failures were
    retried and -- worse -- discarded the warm prompt cache on the way. The
    check must key on the failing call itself.
    """
    import tempfile
    import textwrap

    root = Path(tempfile.mkdtemp()) / "site-packages" / "mlx_vlm" / "generate"
    root.mkdir(parents=True)
    module = root / "dispatch.py"
    module.write_text(textwrap.dedent("""
        def roll_cache_back(cache):
            for c in cache:
                c.trim(4)

        def stream_generate(model):
            return model.config
    """), encoding="utf-8")
    namespace: dict = {}
    exec(compile(module.read_text(encoding="utf-8"), str(module), "exec"), namespace)

    class NoTrim:
        pass

    try:
        namespace["roll_cache_back"]([NoTrim()])
    except AttributeError as exc:
        assert MLXVLMAdapter._is_cache_reuse_failure(exc), "the rollback must be retried"

    try:
        namespace["stream_generate"](None)
    except AttributeError as exc:
        # Same module, same exception type, different call: not a cache problem.
        assert not MLXVLMAdapter._is_cache_reuse_failure(exc), \
            "a genuine model error must not be treated as a cache failure"


def test_genuine_model_errors_are_not_retried_as_cache_failures():
    """Only the reuse machinery earns a second attempt."""
    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    attempts = {"count": 0}

    def stream_generate(**_kwargs):
        attempts["count"] += 1
        raise RuntimeError("the model itself is broken")
        yield

    mlx_vlm.stream_generate = stream_generate
    prompt_utils.apply_chat_template = lambda *_args, **_kwargs: "templated"
    adapter = MLXVLMAdapter()
    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter.prompt_cache_state = object()
    with patch.dict(sys.modules, {"mlx_vlm": mlx_vlm, "mlx_vlm.prompt_utils": prompt_utils}):
        try:
            list(adapter.stream("request", {"prompt": "hello"}))
        except RuntimeError as exc:
            assert "the model itself is broken" in str(exc)
        else:
            raise AssertionError("a genuine model error must surface")
    assert attempts["count"] == 1
    assert adapter.prompt_cache_reuse_failures == 0


# --------------------------------------------------------------------------
# v1.5.3: live generation rate
# --------------------------------------------------------------------------


class SilentUntilTheEndAdapter(ThreadBoundAdapter):
    """Reproduces a real runtime behaviour: no text until the very end.

    `Qwen3.5-9B-MLX-8bit` held 111 tokens in its detokenizer and emitted one
    delta after 14 seconds. Anything that measures the rate by counting deltas
    reports nothing at all for such a model.
    """

    def stream(self, request_id: str, params: dict):
        from mlx_vlm_worker.adapter import _ProgressTicker, _live_progress
        ticker = _ProgressTicker(params)
        # Must outlast the ticker's 0.5 s floor several times over, or the
        # generation finishes before a single tick is due.
        for index in range(60):
            time.sleep(0.03)
            response = SimpleNamespace(generation_tokens=index + 1,
                                       generation_tps=25.0 + index)
            if index < 59:
                if ticker.due():
                    yield {"type": "token_progress", **_live_progress(response)}
            else:
                yield {"type": "delta", "text": "all of it at once",
                       **_live_progress(response)}


def test_generation_rate_is_reported_even_when_no_text_is_emitted():
    adapter = SilentUntilTheEndAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "silent", "params": {
            "prompt": "hi", "heartbeat_interval_seconds": 0.5}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    progress = [event for event in events if event.get("type") == "progress"]
    assert progress, "no progress reported for a runtime that batches its output"
    assert progress[-1]["generated_tokens"] >= 3
    assert progress[-1]["generation_tps"] > 0
    # The worker's internal tick must not reach the coordinator.
    assert not [event for event in events if event.get("type") == "token_progress"]


class FastRateAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        from mlx_vlm_worker.adapter import _live_progress
        for index in range(8):
            time.sleep(0.02)
            # The runtimes divide by elapsed-since-first-token, so their very
            # first sample is nonsense; it must never be published.
            response = SimpleNamespace(generation_tokens=index + 1,
                                       generation_tps=57280.36 if index == 0 else 40.0)
            yield {"type": "delta", "text": f"t{index}", **_live_progress(response)}


def test_the_runtimes_bogus_first_rate_sample_is_never_published():
    adapter = FastRateAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "fast", "params": {
            "prompt": "hi", "heartbeat_interval_seconds": 0.05}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    rates = [event["generation_tps"] for event in events if event.get("type") == "progress"]
    assert rates, "expected progress events"
    assert max(rates) < 1000, rates


class NoRuntimeCountersAdapter(ThreadBoundAdapter):
    def stream(self, request_id: str, params: dict):
        for index in range(8):
            time.sleep(0.02)
            yield {"type": "delta", "text": f"t{index}"}


def test_rate_still_works_when_the_runtime_reports_no_counters():
    """A future runtime may rename or drop these fields; that must not break it."""
    adapter = NoRuntimeCountersAdapter()
    with TestClient(create_app(adapter)) as client:
        generated = client.post("/generate", json={"request_id": "nocounters", "params": {
            "prompt": "hi", "heartbeat_interval_seconds": 0.05}})
    events = [json.loads(line) for line in generated.text.splitlines() if line]
    progress = [event for event in events if event.get("type") == "progress"]
    assert progress, "the worker must fall back to measuring the rate itself"
    assert progress[-1]["generation_tps"] > 0


def test_live_progress_ignores_anything_it_does_not_recognise():
    from mlx_vlm_worker.adapter import _live_progress

    assert _live_progress(SimpleNamespace(generation_tokens=42, generation_tps=37.4)) == {
        "tokens": 42, "tps": 37.4}
    assert _live_progress(SimpleNamespace()) == {}
    assert _live_progress(SimpleNamespace(generation_tokens="x", generation_tps=None)) == {}
    assert _live_progress(SimpleNamespace(generation_tokens=0, generation_tps=0.0)) == {"tokens": 0}


def test_progress_ticker_throttles_and_clamps_its_interval():
    from mlx_vlm_worker.adapter import _ProgressTicker

    # Half a second is the floor: ticking faster would cost more than the
    # display it feeds is worth.
    assert _ProgressTicker({"heartbeat_interval_seconds": 0.001}).interval == 0.5
    assert _ProgressTicker({"heartbeat_interval_seconds": 999}).interval == 30.0
    assert _ProgressTicker({"heartbeat_interval_seconds": "nonsense"}).interval == 10.0

    ticker = _ProgressTicker({"heartbeat_interval_seconds": 0.5})
    assert ticker.due() is False
    time.sleep(0.55)
    assert ticker.due() is True
    assert ticker.due() is False
