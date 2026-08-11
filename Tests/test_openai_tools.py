from __future__ import annotations

import json
import sys
import tempfile
import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from common.tool_calls import parse_tool_markup  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.supervisor import WorkerSupervisor  # noqa: E402


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                         "required": ["path"]}}}]


class ToolWorker:
    loaded = {"id": "Laguna-S-2.1-oQ2e"}

    def __init__(self):
        self.received = None

    async def generate(self, messages, images, options, request_id):
        self.received = (messages, images, options, request_id)
        yield {"type": "tool_calls", "calls": [{"id": "call_test", "type": "function",
               "function": {"name": "read_file", "arguments": '{"path":"README.md"}'}}]}
        yield {"type": "completed", "finish_reason": "tool_calls"}


def make_client(tmp: Path):
    worker = ToolWorker()
    state = SimpleNamespace(
        settings=SimpleNamespace(data={"api": {"requireToken": False}}),
        workers=worker,
        database=Database(tmp / "state.sqlite3"),
    )
    return TestClient(make_public_app(state)), worker, state.database


def request_body(stream=False):
    return {"model": "Laguna-S-2.1-oQ2e", "stream": stream, "tools": TOOLS,
            "tool_choice": "auto", "messages": [
                {"role": "system", "content": "Use tools when needed."},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call_old", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"old"}'}}]},
                {"role": "tool", "tool_call_id": "call_old", "content": "old contents"},
                {"role": "user", "content": "Read README.md"},
            ]}


def test_non_streaming_tool_response_and_message_history_are_openai_compatible():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        response = client.post("/v1/chat/completions", json=request_body())
        assert response.status_code == 200
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] is None
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "read_file"
        messages, _, options, _ = worker.received
        assert [message["role"] for message in messages] == ["system", "assistant", "tool", "user"]
        assert messages[1]["tool_calls"][0]["id"] == "call_old"
        assert messages[2]["tool_call_id"] == "call_old"
        assert options["tools"] == TOOLS
        assert options["tool_choice"] == "auto"


def test_streaming_tool_calls_use_delta_and_terminal_finish_reason():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        response = client.post("/v1/chat/completions", json=request_body(stream=True))
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        deltas = [event["choices"][0]["delta"] for event in events if "choices" in event]
        assert any(delta.get("tool_calls", [{}])[0].get("id") == "call_test" for delta in deltas)
        assert any(event["choices"][0]["finish_reason"] == "tool_calls" for event in events)
        assert response.text.rstrip().endswith("data: [DONE]")


def test_streaming_chunks_keep_stable_timestamp_and_single_terminal_chunk():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        response = client.post("/v1/chat/completions", json=request_body(stream=True))
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        chunks = [event for event in events if event.get("object") == "chat.completion.chunk"]
        assert len({event["created"] for event in chunks}) == 1
        terminal = [event for event in chunks
                    if event.get("choices") and event["choices"][0].get("finish_reason") is not None]
        assert len(terminal) == 1


def test_invalid_stream_and_token_options_use_openai_error_shape():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        base = {"model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hello"}]}
        invalid = ({"stream_options": {"include_usage": True}}, {"max_tokens": True}, {"stream": "yes"})
        for values in invalid:
            response = client.post("/v1/chat/completions", json={**base, **values})
            assert response.status_code == 422
            assert response.json()["error"]["type"] == "invalid_request_error"


def test_non_object_body_uses_openai_error_shape():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        response = client.post("/v1/chat/completions", json=[])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_specific_function_tool_choice_is_accepted_and_restricted():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body["tool_choice"] = {"type": "function", "function": {"name": "read_file"}}
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["tools"] == TOOLS


def test_malformed_tool_is_rejected_without_internal_error():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        body = request_body()
        body["tools"] = ["invalid"]
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_laguna_tool_markup_is_parsed():
    result = parse_tool_markup(
        '<assistant><think>checking</think><tool_call>read_file'
        '<arg_key>path</arg_key><arg_value>README.md</arg_value></tool_call></assistant>'
    )
    assert result["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"path": "README.md"}
    assert "tool_call" not in result["text"]


def test_api_log_is_bounded_and_does_not_store_request_bodies():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        for index in range(2050):
            database.add_api_log({"request_id": str(index), "method": "POST", "path": "/v1/chat/completions",
                                  "status": 200, "model": "model", "message_count": 1, "tool_count": 1})
        logs = database.list_api_logs(10000)
        assert len(logs) == 2000
        assert logs[0]["request_id"] == "2049"
        assert not {"messages", "tools", "authorization", "response"}.intersection(logs[0])
        assert database.clear_api_logs() == 2000
        assert database.list_api_logs() == []


def test_zcode_oversized_max_tokens_is_clamped_instead_of_rejected():
    class ClampingWorker(WorkerSupervisor):
        async def generate(self, prompt, images, options, request_id=None):
            _, _, self.captured = self._validate_generation(prompt, images, options)
            yield {"type": "completed", "finish_reason": "stop"}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = SettingsStore(root)
        settings.data["api"]["requireToken"] = False
        worker = ClampingWorker(root, settings)
        worker.loaded = {"id": "Laguna-S-2.1-oQ2e", "capabilities": {"modelMaxTokens": 1048576}}
        state = SimpleNamespace(settings=settings, workers=worker, database=Database(root / "state.sqlite3"))
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "max_tokens": 32768,
            "messages": [{"role": "system", "content": "ZCode instructions"},
                         {"role": "user", "content": "x" * 200000}],
        })
        assert response.status_code == 200
        assert worker.captured["max_tokens"] == 8192


def catalog_model(model_id="laguna-id", name="Laguna-S-2.1-oQ2e"):
    return {"id": model_id, "source": "filesystem", "name": name,
            "path": "/tmp/" + name, "provider_key": None, "format": "mlx",
            "engine": "mlx-vlm", "modalities": ["text"], "confidence": 1.0,
            "reason": "test", "size_bytes": 1}


class AutoLoadWorker:
    def __init__(self):
        self.loaded = None
        self.load_calls = 0
        self.received = None

    async def load(self, model):
        self.load_calls += 1
        await asyncio.sleep(0.01)
        self.loaded = {**model, "state": "loaded", "capabilities": {"modelMaxTokens": 32768}}
        return self.loaded

    async def generate(self, messages, images, options, request_id):
        self.received = (messages, images, options, request_id)
        yield {"type": "delta", "text": "ok"}
        yield {"type": "usage", "prompt_tokens": 12, "completion_tokens": 1}
        yield {"type": "completed", "finish_reason": "stop"}

    def effective_max_tokens(self):
        return 8192


def make_autoload_client(tmp: Path):
    settings = SettingsStore(tmp)
    settings.data["api"]["requireToken"] = False
    worker = AutoLoadWorker()
    database = Database(tmp / "state.sqlite3")
    database.replace_models([catalog_model()])
    state = SimpleNamespace(settings=settings, workers=worker, database=database,
                            model_autoload_lock=asyncio.Lock())
    return TestClient(make_public_app(state)), worker, database


def test_api_request_restores_requested_model_after_restart():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, database = make_autoload_client(Path(directory))
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert response.json()["usage"] == {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}
        assert worker.load_calls == 1
        assert database.metadata_value("last_loaded_model_id") == "laguna-id"

        second = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "again"}],
        })
        assert second.status_code == 200
        assert worker.load_calls == 1


def test_open_interpreter_alias_restores_last_model():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, database = make_autoload_client(Path(directory))
        database.set_metadata_value("last_loaded_model_id", "laguna-id")
        response = client.post("/v1/chat/completions", json={
            "model": "openai/x", "messages": [{"role": "user", "content": "hello"}],
            "tools": TOOLS, "tool_choice": "auto", "top_p": 0.9, "frequency_penalty": 0,
            "presence_penalty": 0, "seed": 42, "user": "open-interpreter",
            "temperature": 0.25, "repetition_penalty": 1.12, "repetition_context_size": 96,
        })
        assert response.status_code == 200
        assert worker.load_calls == 1
        options = worker.received[2]
        assert options["temperature"] == 0.25
        assert options["top_p"] == 0.9
        assert options["repetition_penalty"] == 1.12
        assert options["repetition_context_size"] == 96


def test_saved_sampling_defaults_apply_when_openai_request_omits_them():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_autoload_client(Path(directory))
        state_settings = client.app.state.mlxbar.settings
        state_settings.data["generation"].update({
            "defaultTemperature": 0.35, "defaultTopP": 0.8,
            "defaultRepetitionPenalty": 1.1, "repetitionContextSize": 64,
        })
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200
        options = worker.received[2]
        assert options["temperature"] == 0.35
        assert options["top_p"] == 0.8
        assert options["repetition_penalty"] == 1.1
        assert options["repetition_context_size"] == 64


def test_manual_unload_prevents_api_autoload():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, database = make_autoload_client(Path(directory))
        database.set_metadata_value("api_autoload_suspended", "1")
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MODEL_NOT_LOADED"
        assert worker.load_calls == 0


def test_librechat_can_fetch_unloaded_models_and_send_common_fields():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_autoload_client(Path(directory))
        listed = client.get("/v1/models")
        assert listed.status_code == 200
        assert listed.json()["data"][0]["id"] == "Laguna-S-2.1-oQ2e"
        assert listed.json()["data"][0]["loaded"] is False
        retrieved = client.get("/v1/models/Laguna-S-2.1-oQ2e")
        assert retrieved.status_code == 200

        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hello"}],
            "response_format": {"type": "text"}, "top_p": 1, "logprobs": False,
            "service_tier": "auto", "metadata": {"client": "librechat"}, "store": False,
        })
        assert response.status_code == 200
        assert worker.load_calls == 1


def test_streaming_include_usage_emits_openai_usage_chunk():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_autoload_client(Path(directory))
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hello"}],
        })
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        usage_chunks = [event for event in events if event.get("choices") == []]
        assert usage_chunks[-1]["usage"] == {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}
        assert response.text.rstrip().endswith("data: [DONE]")


def test_stream_starts_with_role_and_translates_internal_heartbeat_to_sse_comment():
    class HeartbeatWorker(ToolWorker):
        async def generate(self, *_args, **_kwargs):
            yield {"type": "queue", "state": "waiting", "position": 1}
            yield {"type": "phase", "name": "prefill"}
            yield {"type": "heartbeat", "phase": "prefill"}
            yield {"type": "delta", "text": "ready"}
            yield {"type": "completed", "finish_reason": "stop"}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        worker = HeartbeatWorker()
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}), workers=worker,
            database=Database(root / "state.sqlite3"),
        )
        response = TestClient(make_public_app(state)).post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        data_lines = [line for line in response.text.splitlines() if line.startswith("data: {")]
        first = json.loads(data_lines[0][6:])
        assert first["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        assert response.text.count(": mlxbar keep-alive") == 3
        assert '"content": "ready"' in response.text


def test_multiple_completions_are_rejected_cleanly():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_autoload_client(Path(directory))
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "n": 2,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_full_generation_queue_returns_retryable_http_429_before_stream_starts():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = SettingsStore(root)
        settings.data["api"]["requireToken"] = False
        settings.data["generation"]["maxQueuedRequests"] = 2
        worker = WorkerSupervisor(root, settings)
        worker.loaded = {"id": "laguna-id", "name": "Laguna-S-2.1-oQ2e",
                         "engine": "mlx-vlm", "capabilities": {}}
        worker.queued_requests = {"one": None, "two": None}
        state = SimpleNamespace(settings=settings, workers=worker, database=Database(root / "state.sqlite3"))
        response = TestClient(make_public_app(state)).post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "QUEUE_FULL"
        assert response.json()["error"]["retryable"] is True


def test_stream_log_records_body_duration_and_internal_error_code():
    class SlowErrorWorker(ToolWorker):
        async def generate(self, *_args, **_kwargs):
            yield {"type": "phase", "name": "prefill"}
            await asyncio.sleep(0.05)
            yield {"type": "error", "code": "SYNTHETIC_TIMEOUT",
                   "message": "test", "retryable": True}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Database(root / "state.sqlite3")
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=SlowErrorWorker(), database=database,
        )
        response = TestClient(make_public_app(state)).post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200
        log = database.list_api_logs(1)[0]
        assert log["duration_ms"] >= 40
        assert log["error_code"] == "SYNTHETIC_TIMEOUT"
