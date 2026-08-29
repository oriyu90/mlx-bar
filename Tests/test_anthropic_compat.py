"""Anthropic Messages compatibility under /anthropic (v1.8.0, for Claude Code).

Covers request translation, the streaming event sequence, non-streaming shape,
auth (x-api-key and bearer), the error envelope, stop_reason mapping,
count_tokens, model listing, explicit rejection of unsupported features, and --
critically -- that adding this surface does not disturb the OpenAI one.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from mlxbar.database import Database
from mlxbar.main import make_public_app
from mlxbar.settings import SettingsStore


VERSION_HEADER = {"anthropic-version": "2023-06-01"}
PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)).decode()


class FakeWorker:
    def __init__(self):
        self.loaded = {"id": "local-model", "name": "Local Model", "engine": "mlx-lm"}
        self.received = None
        self.script = [
            {"type": "delta", "text": "Hello"},
            {"type": "delta", "text": " world"},
            {"type": "usage", "prompt_tokens": 11, "completion_tokens": 2},
            {"type": "completed", "finish_reason": "stop"},
        ]

    def find_loaded_model(self, requested):
        return self.loaded

    def loaded_models(self):
        return [self.loaded]

    def raise_if_queue_full(self):
        return None

    async def count_tokens(self, model_id, messages, options):
        self.received = ("count_tokens", model_id, messages, options)
        return {"input_tokens": 42}

    async def generate_for_model(self, model_id, messages, images, options, request_id, image_root=None):
        self.received = ("generate", model_id, messages, images, options)
        for event in self.script:
            yield event


def make_client(tmp: Path, *, require_token=False, anthropic_enabled=True, worker=None):
    settings = SettingsStore(tmp)
    settings.data["api"]["requireToken"] = require_token
    settings.data["api"]["anthropic"] = {"enabled": anthropic_enabled}
    database = Database(tmp / "state.sqlite3")
    database.replace_models([{
        "id": "local-model", "name": "Local Model", "engine": "mlx-lm",
        "format": "mlx", "path": "/models/local", "modalities": ["text"],
        "source": "filesystem", "provider_key": None, "confidence": 1.0,
        "size_bytes": 1 << 30, "reason": "",
    }])
    state = SimpleNamespace(settings=settings, workers=worker or FakeWorker(),
                            database=database)
    import asyncio
    state.model_autoload_lock = asyncio.Lock()
    return TestClient(make_public_app(state)), state


def body(**overrides):
    base = {"model": "local-model", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]}
    base.update(overrides)
    return base


# --- non-streaming --------------------------------------------------------

def test_non_streaming_message_shape_and_no_cache_usage_fields():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        response = client.post("/anthropic/v1/messages", json=body(), headers=VERSION_HEADER)
        assert response.status_code == 200
        assert response.headers.get("request-id", "").startswith("req_")
        payload = response.json()
        assert payload["type"] == "message"
        assert payload["role"] == "assistant"
        assert payload["content"] == [{"type": "text", "text": "Hello world"}]
        assert payload["stop_reason"] == "end_turn"
        assert set(payload["usage"]) == {"input_tokens", "output_tokens"}
        assert payload["usage"]["input_tokens"] == 11


def test_system_and_text_blocks_are_translated_for_the_worker():
    with tempfile.TemporaryDirectory() as directory:
        worker = FakeWorker()
        client, _ = make_client(Path(directory), worker=worker)
        client.post("/anthropic/v1/messages", json=body(
            system="be terse",
            messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        ), headers=VERSION_HEADER)
        _, _, messages, _, options = worker.received
        assert messages[0] == {"role": "system", "content": "be terse"}
        assert messages[1] == {"role": "user", "content": "hello"}
        assert options["max_tokens"] == 64


def test_tool_use_and_tool_result_round_trip_to_internal_transcript():
    with tempfile.TemporaryDirectory() as directory:
        worker = FakeWorker()
        client, _ = make_client(Path(directory), worker=worker)
        client.post("/anthropic/v1/messages", json=body(messages=[
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "contents"}]},
        ]), headers=VERSION_HEADER)
        _, _, messages, _, _ = worker.received
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant", "tool"]
        assert messages[1]["tool_calls"][0]["function"]["name"] == "read"
        assert messages[2] == {"role": "tool", "tool_call_id": "toolu_1", "content": "contents"}


def test_tools_and_tool_choice_variants_map_to_openai_shape():
    with tempfile.TemporaryDirectory() as directory:
        worker = FakeWorker()
        client, _ = make_client(Path(directory), worker=worker)
        tools = [{"name": "read", "description": "read", "input_schema": {"type": "object"}}]
        for choice, expected in (
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            ({"type": "tool", "name": "read"}, {"type": "function", "function": {"name": "read"}}),
        ):
            client.post("/anthropic/v1/messages",
                        json=body(tools=tools, tool_choice=choice), headers=VERSION_HEADER)
            assert worker.received[4]["tool_choice"] == expected


def test_image_block_reaches_the_private_workspace():
    with tempfile.TemporaryDirectory() as directory:
        worker = FakeWorker()
        client, _ = make_client(Path(directory), worker=worker)
        client.post("/anthropic/v1/messages", json=body(messages=[{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": PNG_1PX}},
        ]}]), headers=VERSION_HEADER)
        _, _, _, images, _ = worker.received
        assert len(images) == 1 and images[0].endswith(".png")
        assert "mlxbar-images-" in images[0]


# --- streaming ----------------------------------------------------------

def _events(text):
    frames = []
    for chunk in text.split("\n\n"):
        if "data:" not in chunk:
            continue
        data = chunk.split("data:", 1)[1].strip()
        frames.append(json.loads(data))
    return frames


def test_streaming_event_sequence_has_no_done_and_ends_with_message_stop():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        response = client.post("/anthropic/v1/messages", json=body(stream=True),
                               headers=VERSION_HEADER)
        assert response.status_code == 200
        assert "[DONE]" not in response.text
        types = [e["type"] for e in _events(response.text)]
        assert types[0] == "message_start"
        assert "content_block_start" in types
        assert "content_block_delta" in types
        assert types[-2:] == ["message_delta", "message_stop"]


def test_streaming_tool_use_uses_input_json_delta_and_tool_use_stop_reason():
    class ToolWorker(FakeWorker):
        def __init__(self):
            super().__init__()
            self.script = [
                {"type": "tool_calls", "calls": [{"id": "toolu_x", "type": "function",
                 "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
                {"type": "completed", "finish_reason": "tool_calls"},
            ]

    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory), worker=ToolWorker())
        response = client.post("/anthropic/v1/messages", json=body(stream=True),
                               headers=VERSION_HEADER)
        frames = _events(response.text)
        starts = [f for f in frames if f["type"] == "content_block_start"]
        assert starts[0]["content_block"]["type"] == "tool_use"
        assert any(f["type"] == "content_block_delta"
                   and f["delta"]["type"] == "input_json_delta" for f in frames)
        delta = next(f for f in frames if f["type"] == "message_delta")
        assert delta["delta"]["stop_reason"] == "tool_use"


def test_stop_sequence_finish_maps_to_stop_reason_stop_sequence():
    class StopWorker(FakeWorker):
        def __init__(self):
            super().__init__()
            self.script = [
                {"type": "delta", "text": "answer "},
                {"type": "completed", "finish_reason": "stop", "stop_sequence": "END"},
            ]

    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory), worker=StopWorker())
        payload = client.post("/anthropic/v1/messages", json=body(stop_sequences=["END"]),
                              headers=VERSION_HEADER).json()
        assert payload["stop_reason"] == "stop_sequence"
        assert payload["stop_sequence"] == "END"


def test_length_finish_maps_to_max_tokens():
    class LenWorker(FakeWorker):
        def __init__(self):
            super().__init__()
            self.script = [{"type": "delta", "text": "x"},
                           {"type": "completed", "finish_reason": "length"}]

    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory), worker=LenWorker())
        payload = client.post("/anthropic/v1/messages", json=body(), headers=VERSION_HEADER).json()
        assert payload["stop_reason"] == "max_tokens"


# --- auth / errors ----------------------------------------------------------

def test_x_api_key_and_bearer_are_both_accepted():
    with tempfile.TemporaryDirectory() as directory:
        client, state = make_client(Path(directory), require_token=True)
        token = state.settings.api_token
        for headers in ({"x-api-key": token}, {"authorization": f"Bearer {token}"}):
            response = client.post("/anthropic/v1/messages", json=body(),
                                   headers={**VERSION_HEADER, **headers})
            assert response.status_code == 200
        bad = client.post("/anthropic/v1/messages", json=body(),
                          headers={**VERSION_HEADER, "x-api-key": "wrong"})
        assert bad.status_code == 401
        assert bad.json()["error"]["type"] == "authentication_error"


def test_missing_anthropic_version_header_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        response = client.post("/anthropic/v1/messages", json=body())
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"


def test_unsupported_features_are_rejected_explicitly():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        for payload in (
            body(thinking={"type": "enabled", "budget_tokens": 1024}),
            body(tools=[{"name": "web", "type": "web_search_20250305"}]),
            body(messages=[{"role": "user", "content": [{"type": "document", "source": {}}]}]),
        ):
            response = client.post("/anthropic/v1/messages", json=payload, headers=VERSION_HEADER)
            assert response.status_code == 400
            assert response.json()["type"] == "error"


def test_missing_max_tokens_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        payload = {"model": "local-model", "messages": [{"role": "user", "content": "hi"}]}
        response = client.post("/anthropic/v1/messages", json=payload, headers=VERSION_HEADER)
        assert response.status_code == 400


def test_disabled_flag_makes_anthropic_paths_404():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory), anthropic_enabled=False)
        response = client.post("/anthropic/v1/messages", json=body(), headers=VERSION_HEADER)
        assert response.status_code == 404


# --- count_tokens / models ------------------------------------------------

def test_count_tokens_returns_real_worker_count():
    with tempfile.TemporaryDirectory() as directory:
        worker = FakeWorker()
        client, _ = make_client(Path(directory), worker=worker)
        response = client.post("/anthropic/v1/messages/count_tokens", json={
            "model": "local-model", "messages": [{"role": "user", "content": "hi"}],
        }, headers=VERSION_HEADER)
        assert response.status_code == 200
        assert response.json() == {"input_tokens": 42}
        assert worker.received[0] == "count_tokens"


def test_models_listing_is_in_anthropic_shape():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        response = client.get("/anthropic/v1/models", headers=VERSION_HEADER)
        assert response.status_code == 200
        payload = response.json()
        assert payload["has_more"] is False
        assert payload["data"][0]["type"] == "model"
        assert payload["data"][0]["id"] == "Local Model"
        one = client.get("/anthropic/v1/models/Local Model", headers=VERSION_HEADER)
        assert one.status_code == 200
        missing = client.get("/anthropic/v1/models/nope", headers=VERSION_HEADER)
        assert missing.status_code == 404
        assert missing.json()["error"]["type"] == "not_found_error"


# --- isolation from the OpenAI surface ----------------------------------

def test_openai_surface_is_unchanged_by_the_anthropic_mount():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory))
        models = client.get("/v1/models")
        assert models.status_code == 200
        assert models.json()["object"] == "list"
        # OpenAI errors keep their own shape (no Anthropic "type":"error" envelope).
        bad = client.post("/v1/chat/completions", json={"model": "local-model", "messages": []})
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "INVALID_REQUEST"


def test_openai_and_anthropic_requests_share_the_same_generation_backend():
    """Both surfaces route to the same worker; neither blocks or reshapes the
    other's response."""
    SCRIPT = [{"type": "delta", "text": "shared"}, {"type": "completed", "finish_reason": "stop"}]

    class DualWorker(FakeWorker):
        async def generate(self, messages, images, options, request_id, image_root=None):
            for event in SCRIPT:
                yield event

        async def generate_for_model(self, model_id, messages, images, options, request_id,
                                     image_root=None):
            for event in SCRIPT:
                yield event

    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_client(Path(directory), worker=DualWorker())
        anthropic = client.post("/anthropic/v1/messages", json=body(), headers=VERSION_HEADER)
        openai = client.post("/v1/chat/completions",
                             json={"model": "local-model", "max_tokens": 8,
                                   "messages": [{"role": "user", "content": "hi"}]})
        assert anthropic.status_code == 200 and openai.status_code == 200
        assert anthropic.json()["type"] == "message"
        assert openai.json()["object"] == "chat.completion"
        assert anthropic.json()["content"][0]["text"] == "shared"
        assert openai.json()["choices"][0]["message"]["content"] == "shared"
