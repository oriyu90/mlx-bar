from __future__ import annotations

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
