from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402


class TextWorker:
    """Echoes a canned reply; raw string prompts skip the messages branch entirely."""
    loaded = {"id": "Laguna-S-2.1-oQ2e"}

    def __init__(self, text="hello world", *, fail_first_n=0):
        self.text = text
        self.calls: list = []
        self.fail_first_n = fail_first_n

    async def generate(self, prompt, images, options, request_id, image_root=None):
        self.calls.append((prompt, images, options, request_id))
        if len(self.calls) <= self.fail_first_n:
            yield {"type": "error", "code": "GENERATION_FAILED", "message": "boom"}
            return
        text = self.text if isinstance(self.text, str) else self.text[len(self.calls) - 1]
        yield {"type": "delta", "text": text}
        yield {"type": "completed", "finish_reason": "stop"}
        yield {"type": "usage", "prompt_tokens": 10, "completion_tokens": 5}


class ThinkingWorker:
    def __init__(self):
        self.loaded = {"id": "local-model", "name": "Local Model", "engine": "mlx-lm"}
        self.received_options = None

    def find_loaded_model(self, requested):
        return self.loaded

    def loaded_models(self):
        return [self.loaded]

    def raise_if_queue_full(self):
        return None

    async def generate_for_model(self, model_id, messages, images, options, request_id, image_root=None):
        self.received_options = options
        yield {"type": "reasoning_delta", "text": "let me think... "}
        yield {"type": "reasoning_delta", "text": "the answer is 4."}
        yield {"type": "delta", "text": "4"}
        yield {"type": "completed", "finish_reason": "stop"}
        yield {"type": "usage", "prompt_tokens": 10, "completion_tokens": 5}


def make_openai_client(tmp: Path, worker):
    state = SimpleNamespace(
        settings=SimpleNamespace(data={"api": {"requireToken": False}}),
        workers=worker,
        database=Database(tmp / "state.sqlite3"),
    )
    return TestClient(make_public_app(state))


def make_anthropic_client(tmp: Path, worker):
    settings = SettingsStore(tmp)
    settings.data["api"]["requireToken"] = False
    settings.data["api"]["anthropic"] = {"enabled": True}
    database = Database(tmp / "state.sqlite3")
    database.replace_models([{
        "id": "local-model", "name": "Local Model", "engine": "mlx-lm",
        "format": "mlx", "path": "/models/local", "modalities": ["text"],
        "source": "filesystem", "provider_key": None, "confidence": 1.0,
        "size_bytes": 1 << 30, "reason": "",
    }])
    state = SimpleNamespace(settings=settings, workers=worker, database=database)
    state.model_autoload_lock = asyncio.Lock()
    return TestClient(make_public_app(state))


# --- /v1/completions ------------------------------------------------------

def test_legacy_completions_non_stream_uses_raw_prompt():
    with tempfile.TemporaryDirectory() as directory:
        worker = TextWorker("PONG")
        client = make_openai_client(Path(directory), worker)
        response = client.post("/v1/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "prompt": "ping", "max_tokens": 16})
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "text_completion"
        assert body["choices"][0]["text"] == "PONG"
        assert body["choices"][0]["logprobs"] is None
        prompt, images, options, request_id = worker.calls[0]
        assert prompt == "ping"  # no chat template wrapping
        assert options["tools"] == []


def test_legacy_completions_stream_emits_text_completion_chunks():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker("PONG"))
        response = client.post("/v1/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "prompt": "ping", "stream": True})
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        assert any(event["choices"][0]["text"] == "PONG" for event in events)
        assert response.text.rstrip().endswith("data: [DONE]")


def test_legacy_completions_rejects_array_prompt_and_echo():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker())
        base = {"model": "Laguna-S-2.1-oQ2e"}
        response = client.post("/v1/completions", json={**base, "prompt": ["a", "b"]})
        assert response.status_code == 422
        response = client.post("/v1/completions", json={**base, "prompt": "ping", "echo": True})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"


# --- n > 1 ------------------------------------------------------------

def test_chat_completions_n_greater_than_one_returns_multiple_choices():
    with tempfile.TemporaryDirectory() as directory:
        worker = TextWorker(["first", "second", "third"])
        client = make_openai_client(Path(directory), worker)
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "n": 3,
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        body = response.json()
        texts = [choice["message"]["content"] for choice in body["choices"]]
        assert texts == ["first", "second", "third"]
        assert [choice["index"] for choice in body["choices"]] == [0, 1, 2]
        # usage.completion_tokens should sum across choices, prompt_tokens counted once
        assert body["usage"]["completion_tokens"] == 15
        assert body["usage"]["prompt_tokens"] == 10


def test_chat_completions_rejects_n_greater_than_one_with_stream():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker())
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "n": 2, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"


def test_chat_completions_rejects_n_out_of_range():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker())
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "n": 9,
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 422


# --- response_format ----------------------------------------------------

def test_json_object_mode_accepts_valid_json_and_injects_instruction():
    with tempfile.TemporaryDirectory() as directory:
        worker = TextWorker('{"ok": true}')
        client = make_openai_client(Path(directory), worker)
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": "reply"}]})
        assert response.status_code == 200
        assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"ok": True}
        messages, _, _, _ = worker.calls[0]
        assert messages[0]["role"] == "system"
        assert "JSON" in messages[0]["content"]


def test_json_object_mode_rejects_non_json_output():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker("not json at all"))
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": "reply"}]})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "RESPONSE_FORMAT_INVALID"
        assert response.json()["error"]["retryable"] is True


def test_json_schema_mode_validates_against_schema():
    schema = {"type": "object", "properties": {"name": {"type": "string"}},
              "required": ["name"], "additionalProperties": False}
    with tempfile.TemporaryDirectory() as directory:
        good = make_openai_client(Path(directory), TextWorker('{"name": "mlxbar"}'))
        response = good.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "response_format": {"type": "json_schema", "json_schema": {"name": "person", "schema": schema}},
            "messages": [{"role": "user", "content": "reply"}]})
        assert response.status_code == 200

        bad = make_openai_client(Path(directory), TextWorker('{"age": 3}'))
        response = bad.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "response_format": {"type": "json_schema", "json_schema": {"name": "person", "schema": schema}},
            "messages": [{"role": "user", "content": "reply"}]})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "RESPONSE_FORMAT_INVALID"


def test_json_schema_mode_rejects_unsupported_schema_keywords_up_front():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker())
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "x", "schema": {"oneOf": [{"type": "string"}, {"type": "number"}]}}},
            "messages": [{"role": "user", "content": "reply"}]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"


# --- Anthropic extended thinking ----------------------------------------

def test_anthropic_extended_thinking_non_stream_emits_thinking_block_with_signature():
    with tempfile.TemporaryDirectory() as directory:
        client = make_anthropic_client(Path(directory), ThinkingWorker())
        response = client.post("/anthropic/v1/messages", json={
            "model": "local-model", "max_tokens": 512,
            "thinking": {"type": "enabled", "budget_tokens": 128},
            "messages": [{"role": "user", "content": "what is 2+2?"}]},
            headers={"anthropic-version": "2023-06-01"})
        assert response.status_code == 200
        body = response.json()
        assert body["content"][0]["type"] == "thinking"
        assert body["content"][0]["thinking"] == "let me think... the answer is 4."
        assert body["content"][0]["signature"].startswith("mlxbar-local-unsigned:")
        assert body["content"][1] == {"type": "text", "text": "4"}


def test_anthropic_extended_thinking_streams_thinking_delta_then_signature():
    with tempfile.TemporaryDirectory() as directory:
        client = make_anthropic_client(Path(directory), ThinkingWorker())
        response = client.post("/anthropic/v1/messages", json={
            "model": "local-model", "max_tokens": 512, "stream": True,
            "thinking": {"type": "enabled", "budget_tokens": 128},
            "messages": [{"role": "user", "content": "what is 2+2?"}]},
            headers={"anthropic-version": "2023-06-01"})
        events = []
        for block in response.text.split("\n\n"):
            if block.startswith("event:"):
                _, _, data_line = block.partition("\ndata: ")
                events.append(json.loads(data_line))
        thinking_deltas = [event for event in events
                           if event.get("delta", {}).get("type") == "thinking_delta"]
        signature_deltas = [event for event in events
                            if event.get("delta", {}).get("type") == "signature_delta"]
        assert len(thinking_deltas) == 2
        assert len(signature_deltas) == 1


def test_anthropic_thinking_disabled_by_default_keeps_reasoning_internal():
    with tempfile.TemporaryDirectory() as directory:
        client = make_anthropic_client(Path(directory), ThinkingWorker())
        response = client.post("/anthropic/v1/messages", json={
            "model": "local-model", "max_tokens": 512,
            "messages": [{"role": "user", "content": "what is 2+2?"}]},
            headers={"anthropic-version": "2023-06-01"})
        assert response.status_code == 200
        body = response.json()
        assert all(block["type"] != "thinking" for block in body["content"])


def test_anthropic_thinking_budget_must_be_below_max_tokens():
    with tempfile.TemporaryDirectory() as directory:
        client = make_anthropic_client(Path(directory), ThinkingWorker())
        response = client.post("/anthropic/v1/messages", json={
            "model": "local-model", "max_tokens": 100,
            "thinking": {"type": "enabled", "budget_tokens": 200},
            "messages": [{"role": "user", "content": "hi"}]},
            headers={"anthropic-version": "2023-06-01"})
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"


# --- /v1/responses stub ---------------------------------------------------

def test_responses_api_returns_explicit_unsupported_endpoint():
    with tempfile.TemporaryDirectory() as directory:
        client = make_openai_client(Path(directory), TextWorker())
        response = client.post("/v1/responses", json={"model": "x", "input": "hi"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNSUPPORTED_ENDPOINT"
