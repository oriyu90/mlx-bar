"""Adaptive Hybrid Context Memory -- experimental (DESIGN_v2.2.0.md).

Fakes only; MLX is never imported. The coordinator (tier 1) is exercised end
to end through the OpenAI-compatible surface exactly like
``test_context_compression.py``. The mlx-lm worker tier (tier 2) is exercised
through its pure-Python seams plus a fake model, with the fail-closed contract
(`FallbackToExact` and nothing else) as the central assertion.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

import pytest  # noqa: E402

from mlxbar.api.adaptive_memory import (  # noqa: E402
    adaptive_memory_worker_options,
    maybe_plan_adaptive_memory,
)
from mlxbar.api.adaptive_memory import policy as co_policy  # noqa: E402
from mlxbar.api.adaptive_memory import segment as co_segment  # noqa: E402
from mlxbar.api.adaptive_memory.latent_store import NoteCache, note_key  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402


# ======================================================================
# Settings
# ======================================================================

def test_settings_default_to_disabled_and_safe():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        adaptive = store.data["experimental"]["adaptiveMemory"]
        assert adaptive["enabled"] is False
        assert adaptive["softToken"] is False
        assert adaptive["policy"] == "balanced"
        assert adaptive["fallbackToExact"] is True


@pytest.mark.parametrize("patch", [
    {"policy": "aggressive"},
    {"triggerRatio": 0.2},
    {"triggerRatio": 0.99},
    {"memoryPressureRatio": 1.5},
    {"keepTailMessages": 1},
    {"keepTailMessages": 999},
    {"maxLatentTokens": 8},
    {"maxLatentTokens": 5000},
    {"maxRetrievedSegments": 0},
    {"enabled": "yes"},
    {"softToken": 1},
])
def test_settings_reject_out_of_range(patch):
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        with pytest.raises(ValueError):
            store.update({"experimental": {"adaptiveMemory": patch}})


def test_settings_accept_a_valid_patch():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        result = store.update({"experimental": {"adaptiveMemory": {
            "enabled": True, "policy": "memorySaver", "triggerRatio": 0.7,
            "memoryPressureRatio": 0.8, "keepTailMessages": 12,
            "maxLatentTokens": 128, "maxRetrievedSegments": 4,
            "verbatimProtection": True, "softToken": True}}})
        adaptive = result["experimental"]["adaptiveMemory"]
        assert adaptive["enabled"] is True and adaptive["policy"] == "memorySaver"


def test_adaptive_memory_and_context_compression_are_mutually_exclusive():
    with tempfile.TemporaryDirectory() as directory:
        store = SettingsStore(Path(directory))
        store.update({"contextCompression": {"enabled": True}})
        with pytest.raises(ValueError):
            store.update({"experimental": {"adaptiveMemory": {"enabled": True}}})
        # ... and the reverse order.
        store2 = SettingsStore(Path(directory) / "b")
        store2.update({"experimental": {"adaptiveMemory": {"enabled": True}}})
        with pytest.raises(ValueError):
            store2.update({"contextCompression": {"enabled": True}})


# ======================================================================
# Context Structure Analyzer / Policy (tier 1, pure)
# ======================================================================

def test_verbatim_risk_forces_exact():
    risky = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "assistant", "content": "```python\nprint(1)\n```"},
        {"role": "assistant", "content": "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b"},
        {"role": "user", "content": "open src/app/main.py and Sources/MLXBar/App.swift"},
        {"role": "assistant", "content": "commit 4f2a9c1e8b7d6f5a4c3b2a1908f7e6d5c4b3a291"},
        {"role": "assistant", "content": "run: git rebase --onto main feature~3 feature"},
        {"role": "tool", "tool_call_id": "c1", "content": "42 rows, 3.14159, 2.71828, 1024"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
    ]
    for message in risky:
        assert co_segment.classify(message, verbatim_protection=True) == "exact", message


def test_plain_prose_is_latent_eligible():
    message = {"role": "assistant", "content":
              "We discussed the trade-offs at length and eventually agreed the "
              "simplest option was the most maintainable one for the team."}
    assert co_segment.classify(message, verbatim_protection=True) == "latent"


def test_policy_presets_move_the_latent_cutoff():
    middle = [{"role": "assistant", "content": f"Discussion turn number {i} about design philosophy "
               "and how the team prefers to work together over the long run."} for i in range(20)]
    fidelity = co_policy.label_messages(middle, policy="fidelity", verbatim_protection=True)
    saver = co_policy.label_messages(middle, policy="memorySaver", verbatim_protection=True)
    assert fidelity.count("latent") > saver.count("latent")
    assert saver.count("cold") > fidelity.count("cold")


# ======================================================================
# Content-addressed note cache
# ======================================================================

def test_note_cache_round_trips_and_survives_reopen():
    with tempfile.TemporaryDirectory() as directory:
        key = note_key("canonical", "latent-run", "model-x|1.0", "am1")
        NoteCache(Path(directory)).put(key, "a short note")
        assert NoteCache(Path(directory)).get(key) == "a short note"


def test_note_cache_treats_a_truncated_file_as_a_miss():
    with tempfile.TemporaryDirectory() as directory:
        cache = NoteCache(Path(directory))
        key = note_key("c", "latent-run", "m", "am1")
        cache.put(key, "note")
        (Path(directory) / "adaptive-memory" / "notes" / f"{key}.json").write_text("{ not json",
                                                                                   encoding="utf-8")
        assert NoteCache(Path(directory)).get(key) is None


# ======================================================================
# Planner (tier 1) end to end through the OpenAI surface
# ======================================================================

class AdaptiveWorker:
    """Fake pool supervisor. Records every generation call; `-amnote*` request
    ids are the summarizer sub-calls."""

    loaded = {"id": "model-x"}

    def __init__(self, note_text="Condensed discussion.", fail_note=False):
        self.note_text = note_text
        self.fail_note = fail_note
        self.calls: list[tuple[str, list[dict], dict]] = []

    async def generate_for_model(self, model_id, messages, images, options, request_id, image_root=None):
        self.calls.append((request_id, messages, options))
        if "-amnote" in request_id:
            if self.fail_note:
                yield {"type": "error", "code": "GENERATION_FAILED", "message": "unavailable"}
                return
            yield {"type": "delta", "text": self.note_text}
            yield {"type": "completed", "finish_reason": "stop"}
            return
        yield {"type": "delta", "text": "final answer"}
        yield {"type": "completed", "finish_reason": "stop"}


def make_client(tmp: Path, worker: AdaptiveWorker, *, enabled: bool, soft_token: bool = False,
                policy: str = "balanced", trigger_ratio: float = 0.5, keep_tail: int = 4,
                max_prompt_characters: int = 400, context_compression: bool = False):
    state = SimpleNamespace(
        settings=SimpleNamespace(
            root=tmp,
            data={
                "api": {"requireToken": False},
                "generation": {"maxPromptCharacters": max_prompt_characters},
                "contextCompression": {"enabled": context_compression, "triggerRatio": 0.7,
                                       "keepTailMessages": 8, "summaryMaxTokens": 800},
                "experimental": {"adaptiveMemory": {
                    "enabled": enabled, "policy": policy, "triggerRatio": trigger_ratio,
                    "memoryPressureRatio": 0.75, "keepTailMessages": keep_tail,
                    "maxLatentTokens": 256, "maxRetrievedSegments": 8,
                    "verbatimProtection": True, "softToken": soft_token,
                    "persistentLatentCache": True, "persistentHybridKV": True,
                    "fallbackToExact": True}},
            }),
        workers=worker,
        database=Database(tmp / "state.sqlite3"),
        last_context_compression=None,
        last_adaptive_memory=None,
    )
    return TestClient(make_public_app(state)), state


def _long_conversation(turns: int = 24) -> list[dict]:
    messages = [{"role": "system", "content": "You are a coding agent."}]
    for i in range(turns):
        messages.append({"role": "user", "content":
                         f"Let us talk through the design of module {i} and why it matters " * 4})
        messages.append({"role": "assistant", "content":
                         f"I think module {i} should stay small and focused, here is my reasoning " * 4})
    messages.append({"role": "user", "content": "What should we do next?"})
    return messages


def body(messages, stream=False):
    return {"model": "model-x", "stream": stream, "messages": messages}


def test_disabled_by_default_leaves_messages_untouched():
    with tempfile.TemporaryDirectory() as directory:
        worker = AdaptiveWorker()
        client, state = make_client(Path(directory), worker, enabled=False, max_prompt_characters=50)
        messages = _long_conversation()
        response = client.post("/v1/chat/completions", json=body(messages))
        assert response.status_code == 200
        assert len(worker.calls) == 1
        assert worker.calls[0][1] == messages
        assert state.last_adaptive_memory is None
        assert "adaptiveMemory" not in worker.calls[0][2]


def test_enabled_condenses_the_middle_and_keeps_the_tail_verbatim():
    with tempfile.TemporaryDirectory() as directory:
        worker = AdaptiveWorker()
        client, state = make_client(Path(directory), worker, enabled=True, trigger_ratio=0.3, keep_tail=4)
        messages = _long_conversation()
        response = client.post("/v1/chat/completions", json=body(messages))
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "final answer"

        note_calls = [c for c in worker.calls if "-amnote" in c[0]]
        real_call = next(c for c in worker.calls if "-amnote" not in c[0])
        assert note_calls, "expected at least one summarizer sub-call"
        real_messages = real_call[1]
        assert real_messages[0] == messages[0]                      # system verbatim
        assert real_messages[-4:] == messages[-4:]                  # tail verbatim
        assert any(m.get("role") == "system" and "condensed" in (m.get("content") or "").lower()
                   for m in real_messages[1:-4])                    # a note landed in the middle
        assert len(real_messages) < len(messages)                   # COLD turns were dropped
        assert state.last_adaptive_memory is not None
        assert state.last_adaptive_memory["compression_ratio"] < 1.0


def test_planning_failure_falls_back_to_the_raw_prompt():
    with tempfile.TemporaryDirectory() as directory:
        worker = AdaptiveWorker(fail_note=True)
        client, state = make_client(Path(directory), worker, enabled=True, trigger_ratio=0.3, keep_tail=4)
        messages = _long_conversation()
        response = client.post("/v1/chat/completions", json=body(messages))
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "final answer"
        real_call = next(c for c in worker.calls if "-amnote" not in c[0])
        # Note generation failed for the run -> that run is kept verbatim, so the
        # real call still holds every original message.
        assert real_call[1] == messages
        assert state.last_adaptive_memory is None


def test_note_cache_hit_skips_the_summarizer_on_a_resend():
    with tempfile.TemporaryDirectory() as directory:
        messages = _long_conversation()
        worker1 = AdaptiveWorker()
        client1, _ = make_client(Path(directory), worker1, enabled=True, trigger_ratio=0.3, keep_tail=4)
        client1.post("/v1/chat/completions", json=body(messages))
        first_notes = len([c for c in worker1.calls if "-amnote" in c[0]])
        assert first_notes >= 1

        worker2 = AdaptiveWorker()
        client2, _ = make_client(Path(directory), worker2, enabled=True, trigger_ratio=0.3, keep_tail=4)
        client2.post("/v1/chat/completions", json=body(messages))
        assert [c for c in worker2.calls if "-amnote" in c[0]] == []  # all served from cache


def test_worker_receives_the_adaptive_options_when_soft_token_is_on():
    with tempfile.TemporaryDirectory() as directory:
        worker = AdaptiveWorker()
        client, _ = make_client(Path(directory), worker, enabled=True, soft_token=True,
                                trigger_ratio=0.3, keep_tail=4)
        client.post("/v1/chat/completions", json=body(_long_conversation()))
        real_call = next(c for c in worker.calls if "-amnote" not in c[0])
        adaptive_options = real_call[2].get("adaptiveMemory")
        assert adaptive_options and adaptive_options["softToken"] is True
        assert adaptive_options["policyVersion"] == "am1"


def test_worker_options_helper_is_none_unless_enabled():
    settings_off = SimpleNamespace(data={"experimental": {"adaptiveMemory": {"enabled": False}}})
    assert adaptive_memory_worker_options(settings_off) is None
    settings_on = SimpleNamespace(data={"experimental": {"adaptiveMemory": {
        "enabled": True, "softToken": True, "policy": "balanced", "maxLatentTokens": 256}}})
    options = adaptive_memory_worker_options(settings_on)
    assert options["enabled"] is True and options["softToken"] is True


# ======================================================================
# Worker soft-token tier (tier 2)
# ======================================================================

from mlx_lm_worker.adaptive_memory import hybrid_cache, hybrid_prefill  # noqa: E402
from mlx_lm_worker.adaptive_memory import policy as wk_policy  # noqa: E402
from mlx_lm_worker.adaptive_memory.latent_store import latent_key  # noqa: E402
from mlx_lm_worker.adaptive_memory.runtime import AdaptiveRuntime, FallbackToExact  # noqa: E402


def test_worker_policy_bands_only_a_long_prompt():
    assert wk_policy.band(200, "balanced") is None
    banding = wk_policy.band(8000, "balanced")
    assert banding is not None
    assert banding.head >= 1
    assert banding.band_len >= wk_policy.MIN_BAND_TOKENS
    assert banding.tail >= 256
    assert banding.head + banding.band_len + banding.tail == 8000


def test_hybrid_cache_key_changes_with_every_fingerprint():
    base = dict(reader_fp="r", writer_fp="w", template_fp="t", policy_version="am1",
                mlx_lm_version="1.0", exact_block_hash="e", latent_block_hash="l")
    key = hybrid_cache.hybrid_key(**base)
    for field in base:
        altered = {**base, field: base[field] + "!"}
        assert hybrid_cache.hybrid_key(**altered) != key


def test_latent_key_is_content_addressed():
    a = latent_key([1, 2, 3], "reader", "writer", "am1")
    assert a == latent_key([1, 2, 3], "reader", "writer", "am1")
    assert a != latent_key([1, 2, 4], "reader", "writer", "am1")
    assert a != latent_key([1, 2, 3], "reader", "writer2", "am1")


class _NoEmbedModel:
    def __call__(self, inputs, cache=None, input_embeddings=None):
        return inputs


class _NoInputEmbeddingsModel:
    def __call__(self, inputs, cache=None):
        return inputs

    model = SimpleNamespace(embed_tokens=lambda ids: ids)


def test_runtime_falls_back_when_reader_rejects_input_embeddings():
    runtime = AdaptiveRuntime(_NoInputEmbeddingsModel(), SimpleNamespace(chat_template=None),
                              {"enabled": True, "softToken": True, "policy": "balanced",
                               "maxLatentTokens": 64})
    with pytest.raises(FallbackToExact):
        runtime.prepare(list(range(4000)))


def test_runtime_falls_back_when_no_embedding_layer():
    runtime = AdaptiveRuntime(_NoEmbedModel(), SimpleNamespace(chat_template=None),
                              {"enabled": True, "softToken": True, "policy": "balanced",
                               "maxLatentTokens": 64})
    with pytest.raises(FallbackToExact):
        runtime.prepare(list(range(4000)))


def test_runtime_falls_back_on_a_short_prompt():
    runtime = AdaptiveRuntime(_NoEmbedModel(), SimpleNamespace(chat_template=None),
                              {"enabled": True, "softToken": True, "policy": "balanced",
                               "maxLatentTokens": 64})
    with pytest.raises(FallbackToExact):
        runtime.prepare([1, 2, 3])


def test_accepts_input_embeddings_probe():
    assert hybrid_prefill.accepts_input_embeddings(_NoEmbedModel()) is True
    assert hybrid_prefill.accepts_input_embeddings(_NoInputEmbeddingsModel()) is False


# ======================================================================
# Fail-safe contract
# ======================================================================

class _ExplodingWorker:
    loaded = {"id": "model-x"}

    def generate_for_model(self, *args, **kwargs):
        raise RuntimeError("worker exploded")


def test_planner_never_raises_on_a_broken_worker():
    import asyncio
    settings = SimpleNamespace(root=Path("/nonexistent"), data={
        "generation": {"maxPromptCharacters": 100},
        "contextCompression": {"enabled": False},
        "experimental": {"adaptiveMemory": {"enabled": True, "policy": "balanced",
                                            "triggerRatio": 0.3, "keepTailMessages": 4,
                                            "maxLatentTokens": 256, "verbatimProtection": True,
                                            "persistentLatentCache": False, "softToken": False}},
    })
    messages = _long_conversation()
    result, summary = asyncio.run(maybe_plan_adaptive_memory(
        _ExplodingWorker(), {"id": "model-x"}, messages, [], settings, "req-1"))
    assert result == messages and summary is None


def test_planner_is_a_noop_on_malformed_input():
    import asyncio
    settings = SimpleNamespace(root=None, data={
        "generation": {"maxPromptCharacters": 100},
        "contextCompression": {"enabled": False},
        "experimental": {"adaptiveMemory": {"enabled": True}},
    })
    for bad in (None, "not a list", [], [{"role": "user", "content": "hi"}]):
        result, summary = asyncio.run(maybe_plan_adaptive_memory(
            object(), {"id": "m"}, bad, [], settings, "r"))
        assert result == bad and summary is None
