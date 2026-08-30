from __future__ import annotations

import json
import sys
import tempfile
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from common.tool_calls import parse_tool_markup  # noqa: E402
from mlxbar.database import API_LOG_PRUNE_INTERVAL, Database  # noqa: E402
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

    async def generate(self, messages, images, options, request_id, image_root=None):
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


def test_zcode_extra_body_chat_template_kwargs_reach_worker():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body["extra_body"] = {"chat_template_kwargs": {
            "enable_thinking": False, "reasoning_effort": "high",
        }}
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["chat_template_kwargs"] == {
            "enable_thinking": False, "reasoning_effort": "high",
        }


def test_zcode_thinking_and_reasoning_effort_are_normalized_for_qwen():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body.update({
            "thinking": {"type": "enabled", "budget_tokens": 4096, "clear_thinking": False},
            "reasoning_effort": "XHIGH",
        })
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["chat_template_kwargs"] == {
            "enable_thinking": True,
            "thinking_budget": 4096,
            "preserve_thinking": True,
            "reasoning_effort": "xhigh",
        }


def test_zcode_extension_variants_do_not_trigger_unsupported_parameter():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body.update({
            "future_client_field": {"ignored": True},
            "extra_body": {
                "thinking": {"type": "enabled", "effort": "XHIGH", "future_option": True},
                "future_provider_field": "ignored",
            },
        })
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["chat_template_kwargs"] == {
            "enable_thinking": True, "reasoning_effort": "xhigh",
        }


def test_explicit_chat_template_kwargs_override_normalized_thinking_values():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body.update({
            "extra_body": {"chat_template_kwargs": {
                "enable_thinking": False, "reasoning_effort": "low",
            }},
            "thinking": {"type": "enabled"},
            "reasoning_effort": "xhigh",
        })
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["chat_template_kwargs"] == {
            "enable_thinking": False, "reasoning_effort": "low",
        }


def test_reasoning_effort_none_disables_thinking_when_not_explicitly_configured():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        body = request_body()
        body["reasoning_effort"] = "none"
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        assert worker.received[2]["chat_template_kwargs"] == {
            "reasoning_effort": "none", "enable_thinking": False,
        }


def test_invalid_or_unsafe_extra_body_is_rejected_before_generation():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        invalid_values = (
            ([], 422),
            ({"chat_template_kwargs": False}, 422),
            ({"chat_template_kwargs": {"tools": []}}, 400),
        )
        for extra_body, expected_status in invalid_values:
            body = request_body()
            body["extra_body"] = extra_body
            response = client.post("/v1/chat/completions", json=body)
            assert response.status_code == expected_status
        assert worker.received is None


def test_invalid_thinking_options_are_rejected_before_generation():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        invalid_values = (
            ({"thinking": []}, 422),
            ({"thinking": {"type": "unknown"}}, 422),
            ({"thinking": {"type": []}}, 422),
            ({"thinking": {"budget_tokens": True}}, 422),
            ({"thinking": {"clear_thinking": "no"}}, 422),
            ({"reasoning_effort": False}, 422),
        )
        for values, expected_status in invalid_values:
            response = client.post("/v1/chat/completions", json={**request_body(), **values})
            assert response.status_code == expected_status
        assert worker.received is None


def test_laguna_tool_markup_is_parsed():
    result = parse_tool_markup(
        '<assistant><think>checking</think><tool_call>read_file'
        '<arg_key>path</arg_key><arg_value>README.md</arg_value></tool_call></assistant>'
    )
    assert result["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(result["tool_calls"][0]["function"]["arguments"]) == {"path": "README.md"}
    assert "tool_call" not in result["text"]
    assert "checking" not in result["text"]


def test_reasoning_markup_is_stripped_even_without_a_tool_call():
    result = parse_tool_markup(
        "<assistant><think>the user just wants a greeting</think>Hello there!</assistant>"
    )
    assert result["tool_calls"] == []
    assert result["text"] == "Hello there!"


def test_api_log_is_bounded_and_does_not_store_request_bodies():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        for index in range(2050):
            database.add_api_log({"request_id": str(index), "method": "POST", "path": "/v1/chat/completions",
                                  "status": 200, "model": "model", "message_count": 1, "tool_count": 1})
        logs = database.list_api_logs(10000)
        # Pruning runs every API_LOG_PRUNE_INTERVAL inserts instead of on every
        # one, so retention is a target the table returns to rather than a hard
        # per-insert cap. It must still stay bounded.
        assert 2000 <= len(logs) <= 2000 + API_LOG_PRUNE_INTERVAL
        assert logs[0]["request_id"] == "2049"
        assert not {"messages", "tools", "authorization", "response"}.intersection(logs[0])
        assert database.clear_api_logs() == len(logs)
        assert database.list_api_logs() == []


def test_legacy_api_log_schema_is_migrated_with_performance_columns():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.sqlite3"
        import sqlite3
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE api_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, method TEXT NOT NULL,
            path TEXT NOT NULL, status INTEGER NOT NULL, duration_ms INTEGER NOT NULL DEFAULT 0,
            model TEXT, stream INTEGER NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0, error_code TEXT,
            client_scope TEXT NOT NULL DEFAULT 'local', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        connection.commit()
        connection.close()
        database = Database(path)
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(api_logs)")}
        assert {"message_chars", "tool_schema_chars", "first_token_ms", "prompt_tokens",
                "cached_tokens", "prompt_tps", "generation_tps", "reasoning_mode",
                "cache_tier", "cold_reason", "shared_prefix_tokens",
                "held_prefix_tokens"} <= columns


def test_cold_reason_is_a_closed_vocabulary():
    """An unknown reason is stored as NULL rather than written through.

    The column exists to explain a slow request, not to carry text from a
    worker into the database. Keeping it closed is what guarantees a future
    runtime cannot turn it into a channel for anything else.
    """
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        database.add_api_log({"method": "POST", "path": "/v1/chat/completions", "status": 200,
                              "cache_tier": "cold", "cold_reason": "cancelled_previous",
                              "shared_prefix_tokens": 120, "held_prefix_tokens": 400})
        database.add_api_log({"method": "POST", "path": "/v1/chat/completions", "status": 200,
                              "cache_tier": "cold", "cold_reason": "the prompt said hello"})
        logs = database.list_api_logs(10)
        assert logs[1]["cold_reason"] == "cancelled_previous"
        assert logs[1]["shared_prefix_tokens"] == 120
        assert logs[0]["cold_reason"] is None


def test_recent_cache_tiers_expose_a_cold_streak_without_the_conversation():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        for tier in ("memory", "memory", "cold", "cold"):
            database.add_api_log({"method": "POST", "path": "/v1/chat/completions",
                                  "status": 200, "cache_tier": tier,
                                  "cold_reason": "reuse_unsupported" if tier == "cold" else None})
        rows = database.recent_cache_tiers()
        assert [row["cache_tier"] for row in rows] == ["cold", "cold", "memory", "memory"]
        assert not {"messages", "tools"}.intersection(rows[0])
        # Scoping to a model keeps another model's streak from being read as
        # this one's.
        database.add_api_log({"method": "POST", "path": "/v1/chat/completions", "status": 200,
                              "model": "other", "cache_tier": "memory"})
        assert [row["cache_tier"] for row in database.recent_cache_tiers(model="other")] == ["memory"]
        assert database.recent_cache_tiers(model="absent") == []


def test_stream_log_records_privacy_safe_prefill_and_cache_metrics():
    class MetricsWorker(ToolWorker):
        async def generate(self, *_args, **_kwargs):
            yield {"type": "delta", "text": "hello"}
            yield {"type": "usage", "prompt_tokens": 4200, "completion_tokens": 3}
            yield {"type": "metrics", "prompt_tokens": 4200, "cached_tokens": 4000,
                   "prompt_tps": 900.5, "generation_tps": 11.25,
                   "cache_tier": "disk"}
            yield {"type": "completed", "finish_reason": "stop"}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Database(root / "state.sqlite3")
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=MetricsWorker(), database=database,
        )
        response = TestClient(make_public_app(state)).post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True, "max_tokens": 64,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "private user text"}],
            "tools": [{"type": "function", "function": {"name": "read",
                      "description": "private tool description", "parameters": {"type": "object"}}}],
        })
        assert response.status_code == 200
        log = database.list_api_logs(1)[0]
        assert log["message_chars"] > 0 and log["tool_schema_chars"] > 0
        assert log["max_tokens"] == 64 and log["reasoning_mode"] == "low"
        assert log["first_token_ms"] is not None
        assert log["prompt_tokens"] == 4200 and log["cached_tokens"] == 4000
        assert log["prompt_tps"] == 900.5 and log["generation_tps"] == 11.25
        assert log["cache_tier"] == "disk"
        assert not {"messages", "tools", "content", "response"}.intersection(log)


def test_rejected_request_records_model_and_error_code_without_body():
    with tempfile.TemporaryDirectory() as directory:
        client, _, database = make_client(Path(directory))
        response = client.post("/v1/chat/completions", json={
            **request_body(), "thinking": {"type": "invalid"},
        })
        assert response.status_code == 422
        log = database.list_api_logs(1)[0]
        assert log["model"] == "Laguna-S-2.1-oQ2e"
        assert log["error_code"] == "INVALID_REQUEST"
        assert not {"messages", "thinking", "response"}.intersection(log)


def test_zcode_oversized_max_tokens_is_clamped_instead_of_rejected():
    class ClampingWorker(WorkerSupervisor):
        async def generate(self, prompt, images, options, request_id=None, image_root=None):
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

    async def generate(self, messages, images, options, request_id, image_root=None):
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


def test_reasoning_delta_uses_openai_compatible_reasoning_content_field():
    class ReasoningWorker(ToolWorker):
        async def generate(self, *_args, **_kwargs):
            yield {"type": "reasoning_delta", "text": "checking"}
            yield {"type": "delta", "text": "answer"}
            yield {"type": "completed", "finish_reason": "stop"}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=ReasoningWorker(), database=Database(root / "state.sqlite3"),
        )
        response = TestClient(make_public_app(state)).post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        deltas = [event["choices"][0]["delta"] for event in events if event.get("choices")]
        assert {"reasoning_content": "checking"} in deltas
        assert {"content": "answer"} in deltas


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


def test_structured_output_is_refused_rather_than_silently_ignored():
    """Ignoring an unknown vendor field is right; ignoring JSON mode is not."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = SettingsStore(root)
        settings.update({"api": {"requireToken": False}})
        supervisor = WorkerSupervisor(root, settings)
        supervisor.loaded = {"id": "m", "name": "m", "engine": "lm-studio"}
        state = SimpleNamespace(settings=settings, workers=supervisor,
                                database=Database(root / "state.sqlite3"))
        app = make_public_app(state)
        with TestClient(app) as client:
            body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
            rejected = client.post("/v1/chat/completions",
                                   json={**body, "response_format": {"type": "json_object"}})
            assert rejected.status_code == 400
            assert rejected.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"
            assert rejected.json()["error"]["param"] == "response_format"

            logprobs = client.post("/v1/chat/completions", json={**body, "logprobs": True})
            assert logprobs.status_code == 400
            assert logprobs.json()["error"]["param"] == "logprobs"

            # The default value is a no-op and must keep working.
            passthrough = client.post("/v1/chat/completions",
                                      json={**body, "response_format": {"type": "text"}})
            assert passthrough.status_code != 400
        state.database.close()


def test_degraded_tool_support_is_recorded_in_the_api_log():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        database.add_api_log({"request_id": "r1", "method": "POST", "path": "/v1/chat/completions",
                              "status": 200, "model": "m", "tool_support": "degraded"})
        database.add_api_log({"request_id": "r2", "method": "POST", "path": "/v1/chat/completions",
                              "status": 200, "model": "m", "tool_support": "nonsense"})
        rows = database.list_api_logs(10)
        assert rows[1]["tool_support"] == "degraded"
        assert rows[0]["tool_support"] is None
        database.close()


class ProgressWorker:
    loaded = {"id": "m", "name": "m", "engine": "mlx-vlm"}
    active_requests: dict = {}
    queued_requests: dict = {}

    async def generate(self, *_args, **_kwargs):
        yield {"type": "progress", "generated_tokens": 40, "generation_tps": 33.2,
               "elapsed_seconds": 1.2}
        yield {"type": "delta", "text": "hello"}
        yield {"type": "completed", "finish_reason": "stop"}


def test_progress_keeps_the_stream_alive_without_entering_the_reply():
    """The rate is for the menu bar; a client must never see it as content."""
    state = SimpleNamespace(settings=SimpleNamespace(data={"api": {"requireToken": False},
                                                           "generation": {}}),
                            workers=ProgressWorker())
    with TestClient(make_public_app(state)) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "m", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert ": mlxbar keep-alive" in response.text
    assert "generated_tokens" not in response.text
    assert "generation_tps" not in response.text
    content = "".join(
        json.loads(line[6:])["choices"][0]["delta"].get("content", "")
        for line in response.text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
        and json.loads(line[6:]).get("choices"))
    assert content == "hello"


# ------------------------------------------ what a client learns about reuse

class ReuseWorker:
    """A worker that reports a warm request the way the mlx workers do."""

    loaded = {"id": "laguna-id", "name": "Laguna-S-2.1-oQ2e"}

    def __init__(self, cached_tokens=None, cache_tier="memory", cold_reason=None):
        self.cached_tokens = cached_tokens
        self.cache_tier = cache_tier
        self.cold_reason = cold_reason

    async def generate(self, messages, images, options, request_id, image_root=None):
        yield {"type": "delta", "text": "ok"}
        yield {"type": "usage", "prompt_tokens": 400, "completion_tokens": 1}
        metrics = {"type": "metrics", "prompt_tokens": 400, "cache_tier": self.cache_tier,
                   "shared_prefix_tokens": 380, "held_prefix_tokens": 402,
                   "prompt_tps": 900.0, "generation_tps": 40.0}
        if self.cached_tokens is not None:
            metrics["cached_tokens"] = self.cached_tokens
        if self.cold_reason is not None:
            metrics["cold_reason"] = self.cold_reason
        yield metrics
        yield {"type": "completed", "finish_reason": "stop"}


def make_reuse_client(tmp: Path, worker):
    state = SimpleNamespace(
        settings=SimpleNamespace(data={"api": {"requireToken": False}}),
        workers=worker,
        database=Database(tmp / "state.sqlite3"),
    )
    return TestClient(make_public_app(state)), state.database


def reuse_body(stream=False):
    body = {"model": "Laguna-S-2.1-oQ2e", "stream": stream,
            "messages": [{"role": "user", "content": "hello"}]}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def test_usage_reports_the_reused_prefix_so_a_client_can_see_its_cache_working():
    """OpenAI-compatible clients read reuse from `prompt_tokens_details`.

    MLXBar already measures it; without this field the caller's own context and
    cost accounting reports every warm request as a full recomputation, and the
    only place the truth appears is MLXBar's own settings window.
    """
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_reuse_client(Path(directory), ReuseWorker(cached_tokens=380))
        usage = client.post("/v1/chat/completions", json=reuse_body()).json()["usage"]
        assert usage["prompt_tokens"] == 400
        assert usage["prompt_tokens_details"] == {"cached_tokens": 380}


def test_streaming_usage_reports_the_reused_prefix_too():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_reuse_client(Path(directory), ReuseWorker(cached_tokens=380))
        response = client.post("/v1/chat/completions", json=reuse_body(stream=True))
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and not line.endswith("[DONE]")]
        usage = [chunk["usage"] for chunk in chunks if chunk.get("usage")]
        assert usage[-1]["prompt_tokens_details"] == {"cached_tokens": 380}


def test_usage_omits_the_reuse_field_when_the_runtime_does_not_measure_it():
    """Absent and zero are different claims, and a client acts on them alike."""
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_reuse_client(Path(directory), ReuseWorker(cached_tokens=None))
        usage = client.post("/v1/chat/completions", json=reuse_body()).json()["usage"]
        assert "prompt_tokens_details" not in usage


def test_a_reported_reuse_can_never_exceed_the_prompt_it_belongs_to():
    with tempfile.TemporaryDirectory() as directory:
        client, _ = make_reuse_client(Path(directory), ReuseWorker(cached_tokens=99999))
        usage = client.post("/v1/chat/completions", json=reuse_body()).json()["usage"]
        assert usage["prompt_tokens_details"]["cached_tokens"] == usage["prompt_tokens"]


def test_the_workers_cache_report_reaches_the_api_log():
    """The hop the settings window and the cold-streak warning both depend on.

    Every field here is produced by the worker and consumed by the UI, with the
    request handler as the only thing in between; nothing else notices when one
    of them stops being copied.
    """
    with tempfile.TemporaryDirectory() as directory:
        worker = ReuseWorker(cached_tokens=0, cache_tier="cold", cold_reason="reuse_unsupported")
        client, database = make_reuse_client(Path(directory), worker)
        assert client.post("/v1/chat/completions", json=reuse_body()).status_code == 200
        assert client.post("/v1/chat/completions", json=reuse_body(stream=True)).status_code == 200
        for entry in database.list_api_logs(2):
            assert entry["cache_tier"] == "cold"
            assert entry["cold_reason"] == "reuse_unsupported"
            assert entry["shared_prefix_tokens"] == 380
            assert entry["held_prefix_tokens"] == 402


# --------------------------------------------------- what the catalog offers

def test_the_model_list_omits_rows_the_scanner_could_not_read():
    """A discovery client offers whatever this list contains.

    The component folders of a diffusion model look like MLX checkpoints from
    the outside, so the scanner records them with no engine. They stay in the
    management API, which exists to explain what was skipped, and out of this
    one, which exists to say what can answer a request.
    """
    with tempfile.TemporaryDirectory() as directory:
        settings = SettingsStore(Path(directory))
        settings.data["api"]["requireToken"] = False
        database = Database(Path(directory) / "state.sqlite3")
        database.replace_models([
            catalog_model(),
            {**catalog_model("vae-id", "vae"), "format": "unknown", "engine": None,
             "modalities": []},
        ])
        state = SimpleNamespace(settings=settings, workers=AutoLoadWorker(), database=database,
                                model_autoload_lock=asyncio.Lock())
        client = TestClient(make_public_app(state))
        listed = client.get("/v1/models").json()["data"]
        assert [item["id"] for item in listed] == ["Laguna-S-2.1-oQ2e"]
        assert len(database.list_models()) == 2


def test_the_model_list_states_which_inputs_a_model_accepts():
    with tempfile.TemporaryDirectory() as directory:
        settings = SettingsStore(Path(directory))
        settings.data["api"]["requireToken"] = False
        database = Database(Path(directory) / "state.sqlite3")
        database.replace_models([{**catalog_model(), "modalities": ["text", "image"]}])
        state = SimpleNamespace(settings=settings, workers=AutoLoadWorker(), database=database,
                                model_autoload_lock=asyncio.Lock())
        client = TestClient(make_public_app(state))
        assert client.get("/v1/models").json()["data"][0]["modalities"] == ["text", "image"]


def _many_tools(count: int) -> list[dict]:
    return [{"type": "function", "function": {
        "name": f"tool_{index}", "description": "x",
        "parameters": {"type": "object", "properties": {}}}} for index in range(count)]


def test_tool_list_boundary_is_128_entries():
    """OpenClaw-style clients pile up MCP tools; 128 is the documented ceiling."""
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        base = {"model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hi"}]}
        accepted = client.post("/v1/chat/completions", json={**base, "tools": _many_tools(128)})
        assert accepted.status_code == 200
        rejected = client.post("/v1/chat/completions", json={**base, "tools": _many_tools(129)})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "INVALID_REQUEST"


class _TwoModelPool:
    """Minimal stand-in for ModelPoolSupervisor's OpenAI-facing surface."""

    def __init__(self):
        self.residents = {name: {"id": name, "name": name, "engine": "mlx-lm"}
                          for name in ("model-a", "model-b")}
        self.gates = {name: asyncio.Event() for name in self.residents}
        self.entered = {name: asyncio.Event() for name in self.residents}
        self.routed: list[str] = []
        self.loaded = self.residents["model-a"]

    def find_loaded_model(self, requested):
        return self.residents.get(requested.strip())

    def loaded_models(self):
        return list(self.residents.values())

    def raise_if_queue_full(self):
        return None

    async def generate_for_model(self, model_id, messages, images, options, request_id,
                                 image_root=None):
        self.routed.append(model_id)
        self.entered[model_id].set()
        await self.gates[model_id].wait()
        yield {"type": "delta", "text": f"hi from {model_id}"}
        yield {"type": "completed", "finish_reason": "stop"}


def _two_model_state(root: Path):
    return SimpleNamespace(
        settings=SimpleNamespace(data={"api": {"requireToken": False},
                                       "models": {"autoLoadOnAPIRequest": True}}),
        workers=_TwoModelPool(), database=Database(root / "state.sqlite3"))


def test_chat_completions_route_to_the_requested_resident_model():
    with tempfile.TemporaryDirectory() as directory:
        state = _two_model_state(Path(directory))
        for gate in state.workers.gates.values():
            gate.set()
        client = TestClient(make_public_app(state))
        for name in ("model-b", "model-a"):
            body = {"model": name, "messages": [{"role": "user", "content": "hi"}]}
            response = client.post("/v1/chat/completions", json=body)
            assert response.status_code == 200
            assert response.json()["model"] == name
        assert state.workers.routed == ["model-b", "model-a"]


def test_two_resident_models_stream_without_api_layer_serialisation():
    """Both requests must reach their worker before either is allowed to finish."""
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            state = _two_model_state(Path(directory))
            pool = state.workers
            transport = httpx.ASGITransport(app=make_public_app(state))
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                async def run(name):
                    body = {"model": name, "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}
                    async with client.stream("POST", "/v1/chat/completions", json=body) as r:
                        return [line async for line in r.aiter_lines()]

                task_a = asyncio.create_task(run("model-a"))
                task_b = asyncio.create_task(run("model-b"))
                await asyncio.wait_for(
                    asyncio.gather(pool.entered["model-a"].wait(),
                                   pool.entered["model-b"].wait()), timeout=2)
                # Neither has been released yet: proves no shared API-layer lock.
                pool.gates["model-a"].set()
                pool.gates["model-b"].set()
                lines_a, lines_b = await asyncio.wait_for(
                    asyncio.gather(task_a, task_b), timeout=2)
                assert any("hi from model-a" in line for line in lines_a)
                assert any("hi from model-b" in line for line in lines_b)

    asyncio.run(scenario())


def test_real_pool_behind_openai_api_streams_two_models_concurrently():
    """End-to-end: OpenAI API -> real ModelPoolSupervisor -> two fake workers."""
    from unittest.mock import patch as _patch

    import test_model_pool
    from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor

    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 3, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with _patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                for name in ("model-a", "model-b"):
                    await pool.load({"id": name, "name": name, "engine": "mlx-lm",
                                     "size_bytes": GIB}, pin=True)
                for name in ("model-a", "model-b"):
                    pool._slots[name].worker.gen_release = asyncio.Event()
                state = SimpleNamespace(settings=store, workers=pool,
                                        database=Database(root / "state.sqlite3"),
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        async def run(name):
                            body = {"model": name, "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]}
                            async with client.stream("POST", "/v1/chat/completions",
                                                     json=body) as response:
                                assert response.status_code == 200
                                return [line async for line in response.aiter_lines()]

                        task_a = asyncio.create_task(run("model-a"))
                        task_b = asyncio.create_task(run("model-b"))
                        for _ in range(200):
                            await asyncio.sleep(0.01)
                            if (pool._slots["model-a"].worker.active_requests
                                    and pool._slots["model-b"].worker.active_requests):
                                break
                        # Both models are generating at the same instant.
                        assert pool._gen_active_lanes == 2
                        pool._slots["model-a"].worker.gen_release.set()
                        pool._slots["model-b"].worker.gen_release.set()
                        lines_a, lines_b = await asyncio.wait_for(
                            asyncio.gather(task_a, task_b), timeout=3)
                    # Each stream is tagged with the model it was routed to.
                    assert any('"model": "model-a"' in line for line in lines_a)
                    assert all('"model": "model-b"' not in line for line in lines_a)
                    assert any('"model": "model-b"' in line for line in lines_b)
                    assert "data: [DONE]" in lines_a
                    assert "data: [DONE]" in lines_b
                    assert pool._gen_active_lanes == 0
                    assert pool._gen_slots._value == 2
                finally:
                    await pool.shutdown()

    asyncio.run(scenario())


def test_a_second_distinct_model_actually_loads_when_the_pool_allows_it():
    """Sequential API calls must be able to reach a second resident model.

    _ensure_requested_model has a shortcut: with exactly one model resident,
    a request for a name that matches none of the residents is treated as an
    alias for that sole resident rather than autoloaded, so a caller using a
    slightly different name for the same model doesn't get ENGINE_BUSY. But
    if the pool's maxResidentModels allows more than one resident and the
    requested name is an actual, distinct, catalog-known model, this shortcut
    must NOT swallow the request -- otherwise the pool can never grow past one
    resident through ordinary sequential API calls, no matter how high
    maxResidentModels is configured. Reproduces the mlx-bar issue found via
    OpenClaw integration testing on 2026-08-30: with one model already
    resident, requesting a second, different model silently kept answering
    with the first one, both id-and-content-matched.
    """
    from unittest.mock import patch as _patch

    import test_model_pool
    from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor

    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 2, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([
                catalog_model("model-a", "model-a"),
                catalog_model("model-b", "model-b"),
            ])
            with _patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        async def ask(name):
                            body = {"model": name,
                                    "messages": [{"role": "user", "content": "hi"}]}
                            response = await client.post("/v1/chat/completions", json=body)
                            assert response.status_code == 200
                            return response.json()["model"]

                        # Nothing resident yet: loads model-a. Unambiguous.
                        assert await ask("model-a") == "model-a"
                        # Exactly one resident (model-a), but "model-b" names a
                        # real, distinct, catalog-known model and the pool has
                        # room for a second resident -- this must load model-b,
                        # not silently keep answering as model-a.
                        assert await ask("model-b") == "model-b"
                        assert {slot for slot in pool._slots} == {"model-a", "model-b"}
                finally:
                    await pool.shutdown()

    asyncio.run(scenario())


def test_an_unknown_model_is_missing_rather_than_busy_while_another_request_runs():
    """A typo is permanent; ENGINE_BUSY invites the client to retry it forever."""
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_autoload_client(Path(directory))
        assert client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "messages": [{"role": "user", "content": "hi"}],
        }).status_code == 200
        worker.active_requests = {"someone-elses-request": object()}
        response = client.post("/v1/chat/completions", json={
            "model": "no-such-model", "messages": [{"role": "user", "content": "hi"}],
        })
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


# --- v1.7.1: OpenAI-compatible client interop ---------------------------------

def test_omitted_max_tokens_falls_back_to_the_configured_ceiling():
    """Agent clients (Zed, Cline, OpenCode) routinely omit max_tokens; a fixed
    512 truncated them. Fall back to the worker's effective ceiling instead."""
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        worker.effective_max_tokens = lambda: 4096
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert worker.received[2]["max_tokens"] == 4096


def test_explicit_max_tokens_is_still_honoured():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))
        worker.effective_max_tokens = lambda: 4096
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e", "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert worker.received[2]["max_tokens"] == 32


def test_max_tokens_without_a_worker_ceiling_still_defaults_to_512():
    with tempfile.TemporaryDirectory() as directory:
        client, worker, _ = make_client(Path(directory))  # ToolWorker has no ceiling
        response = client.post("/v1/chat/completions", json={
            "model": "Laguna-S-2.1-oQ2e",
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert worker.received[2]["max_tokens"] == 512


def test_legacy_completions_endpoint_returns_an_openai_shaped_error():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        response = client.post("/v1/completions", json={"model": "x", "prompt": "hi"})
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "UNSUPPORTED_ENDPOINT"
        assert body["error"]["message"]


def test_unknown_path_uses_the_openai_error_shape_not_starlette_default():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        response = client.get("/v1/does-not-exist")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["message"]


def test_every_openai_error_response_carries_a_message():
    with tempfile.TemporaryDirectory() as directory:
        client, _, _ = make_client(Path(directory))
        responses = [
            client.post("/v1/completions", json={}),
            client.get("/v1/missing"),
            client.post("/v1/chat/completions", json={"model": "x", "messages": []}),
            client.post("/v1/chat/completions", json=[]),
        ]
        for response in responses:
            assert response.json()["error"].get("message")


class _DeltaToolWorker:
    loaded = {"id": "Laguna-S-2.1-oQ2e"}

    async def generate(self, messages, images, options, request_id, image_root=None):
        yield {"type": "tool_call_delta", "calls": [
            {"index": 0, "id": "call_x", "function": {"name": "read_file", "arguments": ""}}]}
        yield {"type": "tool_call_delta", "calls": [
            {"index": 0, "function": {"arguments": '{"path"'}}]}
        yield {"type": "tool_call_delta", "calls": [
            {"index": 0, "function": {"arguments": ':"a"}'}}]}
        yield {"type": "completed", "finish_reason": "tool_calls"}


def test_streaming_tool_call_deltas_send_role_only_on_the_first_chunk():
    """OpenAI sends delta.role once; repeating it trips strict SDK parsers."""
    with tempfile.TemporaryDirectory() as directory:
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=_DeltaToolWorker(),
            database=Database(Path(directory) / "state.sqlite3"))
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json=request_body(stream=True))
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        roles = [event["choices"][0]["delta"].get("role") for event in events
                 if event.get("choices") and "tool_calls" in event["choices"][0]["delta"]]
        assert roles.count("assistant") == 1


class _SingleResidentPool:
    loaded = None

    def __init__(self):
        self.routed = None
        self._resident = {"id": "resident-1", "name": "Resident One", "engine": "mlx-lm"}

    def find_loaded_model(self, requested):
        return None

    def loaded_models(self):
        return [self._resident]

    def raise_if_queue_full(self):
        return None

    async def generate_for_model(self, model_id, messages, images, options, request_id,
                                 image_root=None):
        self.routed = model_id
        yield {"type": "delta", "text": "ok"}
        yield {"type": "completed", "finish_reason": "stop"}


def test_a_made_up_model_name_routes_to_the_sole_resident_instead_of_erroring():
    with tempfile.TemporaryDirectory() as directory:
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False},
                                           "models": {"autoLoadOnAPIRequest": True}}),
            workers=_SingleResidentPool(),
            database=Database(Path(directory) / "state.sqlite3"))
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json={
            "model": "whatever-the-client-configured",
            "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert state.workers.routed == "resident-1"


def test_management_errors_carry_a_japanese_message_not_a_bare_code():
    from mlxbar.main import make_management_app
    with tempfile.TemporaryDirectory() as directory:
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=SimpleNamespace(),
            database=Database(Path(directory) / "state.sqlite3"))
        client = TestClient(make_management_app(state))
        response = client.post("/api/v1/runtimes/not-an-engine/check")
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "INVALID_ENGINE"
        assert detail["message"] and detail["message"] != "INVALID_ENGINE"
