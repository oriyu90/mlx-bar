from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

import pytest  # noqa: E402

from mlxbar.api.context_compression import _split_point  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402


class CompressibleWorker:
    """Fake pool supervisor: one resident model, a configurable char limit.

    Every ``generate_for_model`` call is recorded so a test can tell the
    summarization sub-call (``<request_id>-compact``) apart from the real one.
    """

    loaded = {"id": "model-x"}

    def __init__(self, summary_text: str = "Summary of earlier turns.", fail_summary: bool = False):
        self.summary_text = summary_text
        self.fail_summary = fail_summary
        self.calls: list[tuple[str, list[dict]]] = []

    async def generate_for_model(self, model_id, messages, images, options, request_id, image_root=None):
        self.calls.append((request_id, messages))
        if request_id.endswith("-compact"):
            if self.fail_summary:
                yield {"type": "error", "code": "GENERATION_FAILED", "message": "worker unavailable"}
                return
            yield {"type": "delta", "text": self.summary_text}
            yield {"type": "completed", "finish_reason": "stop"}
            return
        yield {"type": "delta", "text": "final answer"}
        yield {"type": "completed", "finish_reason": "stop"}


def make_client(tmp: Path, worker: CompressibleWorker, *, enabled: bool, trigger_ratio: float = 0.5,
                keep_tail: int = 2, summary_max_tokens: int = 200, max_prompt_characters: int = 400):
    state = SimpleNamespace(
        settings=SimpleNamespace(data={
            "api": {"requireToken": False},
            "generation": {"maxPromptCharacters": max_prompt_characters},
            "contextCompression": {"enabled": enabled, "triggerRatio": trigger_ratio,
                                   "keepTailMessages": keep_tail, "summaryMaxTokens": summary_max_tokens},
        }),
        workers=worker,
        database=Database(tmp / "state.sqlite3"),
        last_context_compression=None,
    )
    return TestClient(make_public_app(state)), state


def _long_conversation(turns: int = 20) -> list[dict]:
    messages = [{"role": "system", "content": "You are a coding agent."}]
    for i in range(turns):
        messages.append({"role": "user", "content": f"Please look at file_{i}.py, it has quite a lot of content " * 5})
        messages.append({"role": "assistant", "content": f"Looked at file_{i}.py, here is what I found " * 5})
    messages.append({"role": "user", "content": "What should we do next?"})
    return messages


def request_body(messages, stream=False, tools=None):
    return {"model": "model-x", "stream": stream, "messages": messages,
            **({"tools": tools, "tool_choice": "auto"} if tools else {})}


def test_disabled_by_default_leaves_messages_untouched():
    with tempfile.TemporaryDirectory() as directory:
        worker = CompressibleWorker()
        client, state = make_client(Path(directory), worker, enabled=False, max_prompt_characters=50)  # tiny -- would trigger if enabled
        messages = _long_conversation()
        response = client.post("/v1/chat/completions", json=request_body(messages))
        assert response.status_code == 200
        assert len(worker.calls) == 1  # no separate summarization call
        _, sent_messages = worker.calls[0]
        assert sent_messages == messages
        assert state.last_context_compression is None


def test_triggers_and_preserves_tool_call_pairing():
    with tempfile.TemporaryDirectory() as directory:
        worker = CompressibleWorker()
        client, state = make_client(Path(directory), worker, enabled=True, trigger_ratio=0.3, keep_tail=2)
        messages = [{"role": "system", "content": "You are a coding agent."}]
        for i in range(10):
            messages.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": "read_file", "arguments": f'{{"path":"f{i}.py"}}'}}]})
            messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": f"contents of f{i}.py " * 20})
        messages.append({"role": "user", "content": "Summarize what we've read so far."})
        response = client.post("/v1/chat/completions", json=request_body(messages))
        assert response.status_code == 200

        assert len(worker.calls) == 2
        compact_call = next(call for call in worker.calls if call[0].endswith("-compact"))
        real_call = next(call for call in worker.calls if not call[0].endswith("-compact"))
        _, real_messages = real_call

        # system prompt preserved verbatim, at the front.
        assert real_messages[0] == messages[0]
        # a synthetic summary message replaced the compressed middle.
        assert any(m.get("role") == "system" and "Summary of earlier turns." in (m.get("content") or "")
                   for m in real_messages[1:])
        # no assistant tool_calls message is immediately followed by something
        # other than its own tool result (i.e. no pairing was split).
        for index, message in enumerate(real_messages):
            if message.get("role") == "assistant" and message.get("tool_calls"):
                assert index + 1 < len(real_messages)
                assert real_messages[index + 1].get("role") == "tool"
                assert real_messages[index + 1]["tool_call_id"] == message["tool_calls"][0]["id"]
        # the compressed prompt is materially shorter than the original.
        assert state.last_context_compression is not None
        assert state.last_context_compression["compressedChars"] < state.last_context_compression["originalChars"]

        # the summarization sub-call never saw the tail (last user turn).
        _, compact_messages = compact_call
        assert all(m.get("content") != "Summarize what we've read so far." for m in compact_messages)


def test_falls_back_to_original_messages_when_summarization_fails():
    with tempfile.TemporaryDirectory() as directory:
        worker = CompressibleWorker(fail_summary=True)
        client, state = make_client(Path(directory), worker, enabled=True, trigger_ratio=0.3, keep_tail=2)
        messages = _long_conversation()
        response = client.post("/v1/chat/completions", json=request_body(messages))
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "final answer"
        assert len(worker.calls) == 2  # the failed summarization attempt, then the real call unmodified
        _, real_messages = worker.calls[-1]
        assert real_messages == messages
        assert state.last_context_compression is None


def test_split_point_keeps_image_turns_out_of_the_compressible_middle():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(6):
        messages.append({"role": "user", "content": f"turn {i}"})
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]})
    messages.append({"role": "assistant", "content": "described the image"})
    messages.append({"role": "user", "content": "final question"})

    split = _split_point(messages, keep_tail=2)
    assert split is not None
    start, tail_start = split
    image_index = next(i for i, m in enumerate(messages)
                       if isinstance(m.get("content"), list)
                       and any(part.get("type") == "image_url" for part in m["content"]))
    assert tail_start <= image_index


def test_split_point_returns_none_when_conversation_is_shorter_than_the_tail():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert _split_point(messages, keep_tail=8) is None


def test_settings_default_to_disabled():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        assert store.data["contextCompression"]["enabled"] is False
        assert store.data["contextCompression"]["triggerRatio"] == 0.7
        assert store.data["contextCompression"]["keepTailMessages"] == 8


def test_settings_reject_out_of_range_trigger_ratio():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        with pytest.raises(ValueError):
            store.update({"contextCompression": {"triggerRatio": 0.2}})


def test_settings_accept_a_valid_patch():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        result = store.update({"contextCompression": {"enabled": True, "triggerRatio": 0.8,
                                                       "keepTailMessages": 12, "summaryMaxTokens": 500}})
        assert result["contextCompression"] == {"enabled": True, "triggerRatio": 0.8,
                                                 "keepTailMessages": 12, "summaryMaxTokens": 500}
