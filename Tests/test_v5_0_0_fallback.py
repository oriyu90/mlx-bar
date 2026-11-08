"""v5.0.0: API fallback to external AI providers (DESIGN_v5.0.0.md).

Three layers are covered:

* pure logic -- settings validation, candidate selection (explicit beats
  wildcard, priority, image filtering), rate windows and cooldowns;
* translation -- what exactly is sent for each of the three protocols;
* integration -- `/v1/chat/completions` and `/anthropic/v1/messages` falling
  back when the local pool refuses, streaming a normalised answer through the
  *existing* encoders, and staying byte-identical to v4.0.2 when disabled.

The HTTP side is exercised with `httpx.MockTransport`, so no network and no
model weights are needed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from mlxbar.errors import MLXBarError
from mlxbar.fallback import config as fallback_config
from mlxbar.fallback.client import FallbackError, non_stream_events, parse_event, probe_provider
from mlxbar.fallback.config import FallbackSettings, normalise_settings, validate_settings
from mlxbar.fallback.limiter import FallbackLimiter
from mlxbar.fallback.selector import select_candidates
from mlxbar.fallback.service import FallbackService, is_trigger
from mlxbar.fallback.translator import (
    anthropic_body, auth_headers, build_body, endpoint_path, estimate_tokens,
    openai_chat_body, openai_responses_body, trim_to_context,
)
from mlxbar.settings import SettingsStore


def provider(provider_id="p-00000001", *, enabled=True, protocol="openai-chat",
             base_url="https://example.test/v1", models=None):
    return {"id": provider_id, "name": "P" + provider_id[-1], "protocol": protocol,
            "baseURL": base_url, "enabled": enabled, "timeoutSeconds": 120,
            "apiKeyConfigured": False, "models": models or []}


def model(model_id="m-00000001", *, name="gpt-x", priority=10, sources=None,
          enabled=True, images=False, max_requests=0, window=60):
    return {"id": model_id, "model": name, "label": "", "enabled": enabled,
            "priority": priority, "sourceModels": sources if sources is not None else ["*"],
            "sourceModelsExplicit": False, "supportsImages": images,
            "maxRequests": max_requests, "rateWindowSeconds": window,
            "maxOutputTokens": 0, "maxContextTokens": 0, "imageDetail": "auto",
            "temperature": None, "topP": None, "extraHeaders": {}}


def settings_doc(providers, *, enabled=True, attempts=3, forward_images=True, cooldown=60):
    return {"enabled": enabled, "cooldownSeconds": cooldown, "maxAttempts": attempts,
            "forwardImagesImmediately": forward_images, "providers": providers}


class SettingsValidationTests(unittest.TestCase):
    def test_defaults_are_disabled_and_empty(self):
        document = normalise_settings({})
        self.assertFalse(document["enabled"])
        self.assertEqual(document["providers"], [])
        self.assertEqual(document["maxAttempts"], 3)

    def test_valid_document_passes(self):
        validate_settings(settings_doc([provider(models=[model()])]))

    def test_provider_id_shape_is_enforced(self):
        with self.assertRaises(ValueError) as caught:
            validate_settings(settings_doc([provider(provider_id="../evil")]))
        self.assertIn("apiFallback.providers[0].id", str(caught.exception))

    def test_duplicate_provider_ids_rejected(self):
        with self.assertRaises(ValueError):
            validate_settings(settings_doc([provider(), provider()]))

    def test_duplicate_model_ids_rejected(self):
        with self.assertRaises(ValueError):
            validate_settings(settings_doc([provider(models=[model(), model()])]))

    def test_protocol_must_be_known(self):
        with self.assertRaises(ValueError) as caught:
            validate_settings(settings_doc([provider(protocol="grpc")]))
        self.assertIn("protocol", str(caught.exception))

    def test_base_url_must_be_http(self):
        with self.assertRaises(ValueError):
            validate_settings(settings_doc([provider(base_url="ftp://x")]))

    def test_priority_range(self):
        bad = model()
        bad["priority"] = 5000
        with self.assertRaises(ValueError) as caught:
            validate_settings(settings_doc([provider(models=[bad])]))
        self.assertIn("priority", str(caught.exception))

    def test_rate_window_range(self):
        bad = model(window=1)
        with self.assertRaises(ValueError):
            validate_settings(settings_doc([provider(models=[bad])]))

    def test_empty_source_models_is_allowed_but_matches_nothing(self):
        document = settings_doc([provider(models=[model(sources=[])])])
        validate_settings(document)
        selected = select_candidates(FallbackSettings.from_document(document), "any")
        self.assertEqual(selected, [])

    def test_extra_headers_cannot_inject_lines(self):
        bad = model()
        bad["extraHeaders"] = {"x-a": "b\r\nx-evil: 1"}
        with self.assertRaises(ValueError):
            validate_settings(settings_doc([provider(models=[bad])]))

    def test_store_rejects_unknown_fallback_key(self):
        with tempfile.TemporaryDirectory() as root:
            store = SettingsStore(root=Path(root))
            with self.assertRaises(ValueError):
                store.update({"apiFallback": {"enabled": True, "bogus": 1}})

    def test_store_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as root:
            store = SettingsStore(root=Path(root))
            store.update({"apiFallback": settings_doc([provider(models=[model()])])})
            reloaded = SettingsStore(root=Path(root))
            self.assertTrue(reloaded.data["apiFallback"]["enabled"])
            self.assertEqual(len(reloaded.data["apiFallback"]["providers"]), 1)

    def test_api_key_never_enters_config_json(self):
        with tempfile.TemporaryDirectory() as root:
            store = SettingsStore(root=Path(root))
            store.update({"apiFallback": settings_doc([provider(models=[model()])])})
            store.set_fallback_api_key("p-00000001", "sk-secret-value")
            text = (Path(root) / "config.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-secret-value", text)
            key_file = Path(root) / "control" / "fallback-p-00000001.key"
            self.assertTrue(key_file.exists())
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_unsafe_provider_id_cannot_become_a_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = SettingsStore(root=Path(root))
            with self.assertRaises(ValueError):
                store.fallback_key_path("../../etc/passwd")


class SelectorTests(unittest.TestCase):
    def settings(self, providers, **kwargs):
        return FallbackSettings.from_document(settings_doc(providers, **kwargs))

    def test_disabled_feature_selects_nothing(self):
        settings = self.settings([provider(models=[model(sources=["*"])])], enabled=False)
        self.assertEqual(select_candidates(settings, "m1"), [])

    def test_explicit_beats_wildcard(self):
        providers = [
            provider("p-00000001", models=[model("m-00000001", sources=["*"], priority=1)]),
            provider("p-00000002", models=[model("m-00000002", sources=["m1"], priority=50)]),
        ]
        selected = select_candidates(self.settings(providers), "m1")
        self.assertEqual([item.model_id for item in selected], ["m-00000002"])

    def test_wildcard_used_when_nothing_is_explicit(self):
        providers = [provider(models=[model(sources=["*"])])]
        self.assertEqual(len(select_candidates(self.settings(providers), "m1")), 1)

    def test_unassigned_model_gets_no_fallback(self):
        providers = [provider(models=[model(sources=["other"])])]
        self.assertEqual(select_candidates(self.settings(providers), "m1"), [])

    def test_priority_order_then_declaration_order(self):
        providers = [
            provider("p-00000001", models=[model("m-00000001", priority=20),
                                           model("m-00000002", priority=5)]),
            provider("p-00000002", models=[model("m-00000003", priority=5)]),
        ]
        selected = select_candidates(self.settings(providers), "m1")
        self.assertEqual([item.model_id for item in selected],
                         ["m-00000002", "m-00000003", "m-00000001"])

    def test_disabled_provider_and_model_are_skipped(self):
        providers = [provider("p-00000001", enabled=False, models=[model("m-00000001")]),
                     provider("p-00000002", models=[model("m-00000002", enabled=False)])]
        self.assertEqual(select_candidates(self.settings(providers), "m1"), [])

    def test_images_require_a_vision_model(self):
        providers = [provider(models=[model("m-00000001", images=False),
                                      model("m-00000002", images=True)])]
        selected = select_candidates(self.settings(providers), "m1", has_images=True)
        self.assertEqual([item.model_id for item in selected], ["m-00000002"])

    def test_images_without_vision_model_fall_back_nowhere(self):
        providers = [provider(models=[model(images=False)])]
        self.assertEqual(select_candidates(self.settings(providers), "m1", has_images=True), [])

    def test_aliases_match_by_name_and_provider_key(self):
        providers = [provider(models=[model(sources=["display name"])])]
        selected = select_candidates(self.settings(providers), "id-1",
                                     aliases=("display name", "provider-key"))
        self.assertEqual(len(selected), 1)

    def test_cooling_and_limited_move_to_the_back(self):
        providers = [provider(models=[model("m-00000001", priority=1),
                                      model("m-00000002", priority=2)])]
        selected = select_candidates(self.settings(providers), "m1",
                                     cooling=["p-00000001/m-00000001"])
        self.assertEqual([item.model_id for item in selected], ["m-00000002", "m-00000001"])

    def test_limit_caps_the_attempts(self):
        providers = [provider(models=[model(f"m-0000000{i}") for i in range(1, 6)])]
        selected = select_candidates(self.settings(providers, attempts=2), "m1", limit=2)
        self.assertEqual(len(selected), 2)


class LimiterTests(unittest.TestCase):
    def test_window_counts_and_expires(self):
        now = [1000.0]
        limiter = FallbackLimiter(clock=lambda: now[0])
        limiter.record_send("k", 60, 2)
        limiter.record_send("k", 60, 2)
        self.assertTrue(limiter.is_limited("k", 2, 60))
        now[0] += 61
        self.assertFalse(limiter.is_limited("k", 2, 60))

    def test_unlimited_models_do_not_accumulate_stamps(self):
        # Review M-3: maxRequests == 0 means unlimited, so nothing is counted.
        limiter = FallbackLimiter()
        for _ in range(10_000):
            limiter.record_send("hot", 60, 0)
        self.assertEqual(limiter.usage("hot", 60), 0)

    def test_one_window_is_capped_even_with_a_huge_limit(self):
        limiter = FallbackLimiter()
        for _ in range(10_000):
            limiter.record_send("hot", 60, 1_000_000)
        self.assertLessEqual(limiter.usage("hot", 60), 4096)

    def test_zero_means_unlimited(self):
        limiter = FallbackLimiter()
        for _ in range(50):
            limiter.record_send("k", 60, 0)
        self.assertFalse(limiter.is_limited("k", 0, 60))

    def test_cooldown_expires(self):
        now = [0.0]
        limiter = FallbackLimiter(clock=lambda: now[0])
        limiter.record_failure("k", "boom")
        self.assertTrue(limiter.is_cooling("k", 60))
        now[0] += 61
        self.assertFalse(limiter.is_cooling("k", 60))

    def test_success_clears_cooldown(self):
        limiter = FallbackLimiter()
        limiter.record_failure("k", "boom")
        limiter.record_send("k", 60, 5)
        self.assertFalse(limiter.is_cooling("k", 60))

    def test_table_is_bounded(self):
        limiter = FallbackLimiter(max_entries=4)
        for index in range(20):
            limiter.record_send(f"k{index}", 60, 5)
        self.assertLessEqual(len(limiter.snapshot()), 4)


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "lookup", "arguments": '{"q":"x"}'}}],
             "content": None},
            {"role": "tool", "tool_call_id": "c1", "content": "42"},
        ]
        self.options = {"max_tokens": 128, "temperature": 0.2, "top_p": 0.9,
                        "tools": [{"type": "function", "function": {
                            "name": "lookup", "description": "d",
                            "parameters": {"type": "object"}}}],
                        "tool_choice": "auto", "stop": ["END"]}

    def test_openai_chat_body(self):
        body = openai_chat_body({"model": "gpt", "imageDetail": "auto"}, self.messages,
                                [], self.options, True)
        self.assertEqual(body["model"], "gpt")
        self.assertEqual(body["max_tokens"], 128)
        self.assertTrue(body["stream"])
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["stop"], ["END"])
        self.assertEqual(body["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(body["messages"][3]["tool_call_id"], "c1")

    def test_openai_chat_attaches_images_to_last_user_turn(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            body = openai_chat_body({"model": "gpt"}, self.messages, [str(path)],
                                    self.options, False)
        parts = body["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "hi"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_missing_image_file_is_skipped_not_fatal(self):
        body = openai_chat_body({"model": "gpt"}, self.messages, ["/nonexistent.png"],
                                self.options, False)
        self.assertEqual(body["messages"][1]["content"], "hi")

    def test_responses_body_moves_system_to_instructions(self):
        body = openai_responses_body({"model": "gpt"}, self.messages, [], self.options, True)
        self.assertEqual(body["instructions"], "be brief")
        self.assertFalse(any(item.get("role") == "system" for item in body["input"]))
        self.assertTrue(any(item.get("type") == "function_call" for item in body["input"]))
        self.assertTrue(any(item.get("type") == "function_call_output" for item in body["input"]))
        self.assertEqual(body["tools"][0]["type"], "function")

    def test_anthropic_body(self):
        body = anthropic_body({"model": "claude"}, self.messages, [], self.options, True)
        self.assertEqual(body["system"], "be brief")
        self.assertEqual(body["stop_sequences"], ["END"])
        self.assertEqual(body["tools"][0]["input_schema"], {"type": "object"})
        assistant = next(turn for turn in body["messages"] if turn["role"] == "assistant")
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        tool_turn = next(turn for turn in body["messages"]
                         if turn["content"] and isinstance(turn["content"], list)
                         and turn["content"][0].get("type") == "tool_result")
        self.assertEqual(tool_turn["content"][0]["tool_use_id"], "c1")

    def test_build_body_dispatch_and_paths(self):
        for protocol, path in (("openai-chat", "/chat/completions"),
                               ("openai-responses", "/responses"),
                               ("anthropic-messages", "/messages")):
            body = build_body(protocol, {"model": "x"}, self.messages, [], self.options, False)
            self.assertEqual(body["model"], "x")
            self.assertEqual(endpoint_path(protocol), path)

    def test_auth_headers_per_protocol(self):
        chat = auth_headers("openai-chat", "k")
        self.assertEqual(chat["authorization"], "Bearer k")
        anthropic = auth_headers("anthropic-messages", "k")
        self.assertEqual(anthropic["x-api-key"], "k")
        self.assertEqual(anthropic["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", anthropic)

    def test_max_output_tokens_is_an_upper_bound(self):
        body = openai_chat_body({"model": "x", "maxOutputTokens": 64}, self.messages, [],
                                {"max_tokens": 1000}, False)
        self.assertEqual(body["max_tokens"], 64)
        body = openai_chat_body({"model": "x", "maxOutputTokens": 4000}, self.messages, [],
                                {"max_tokens": 1000}, False)
        self.assertEqual(body["max_tokens"], 1000)

    def test_temperature_override(self):
        body = openai_chat_body({"model": "x", "temperature": 0.0}, self.messages, [], 
                                {"temperature": 0.9}, False)
        self.assertEqual(body["temperature"], 0.0)

    def test_trim_to_context_drops_oldest_and_keeps_system(self):
        messages = [{"role": "system", "content": "sys"}]
        for index in range(20):
            messages.append({"role": "user", "content": "x" * 400})
        trimmed, dropped = trim_to_context(messages, [], 100)
        self.assertGreater(dropped, 0)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertLessEqual(estimate_tokens(trimmed, []), 100 * 3)

    def test_trim_never_drops_the_last_turn(self):
        messages = [{"role": "user", "content": "x" * 100000}]
        trimmed, dropped = trim_to_context(messages, [], 10)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(trimmed), 1)


class ParserTests(unittest.TestCase):
    def test_openai_chat_stream_chunks(self):
        events = parse_event("openai-chat", {"choices": [{"delta": {"content": "hi"},
                                                          "finish_reason": None}]})
        self.assertEqual(events[0], {"type": "delta", "text": "hi"})

    def test_openai_chat_usage_and_finish(self):
        events = parse_event("openai-chat", {"choices": [{"delta": {},
                                                          "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 3,
                                                       "completion_tokens": 4}})
        kinds = [event["type"] for event in events]
        self.assertIn("completed", kinds)
        self.assertIn("usage", kinds)

    def test_anthropic_text_and_tool_events(self):
        text = parse_event("anthropic-messages", {"type": "content_block_delta",
                                                  "delta": {"type": "text_delta", "text": "yo"}})
        self.assertEqual(text[0], {"type": "delta", "text": "yo"})
        tool = parse_event("anthropic-messages", {"type": "content_block_start", "index": 0,
                                                  "content_block": {"type": "tool_use",
                                                                    "id": "t1", "name": "f"}})
        self.assertEqual(tool[0]["type"], "tool_call_delta")
        self.assertEqual(tool[0]["calls"][0]["function"]["name"], "f")

    def test_responses_events(self):
        delta = parse_event("openai-responses", {"type": "response.output_text.delta",
                                                 "delta": "hey"})
        self.assertEqual(delta[0], {"type": "delta", "text": "hey"})
        done = parse_event("openai-responses", {"type": "response.completed",
                                                "response": {"status": "completed",
                                                             "usage": {"input_tokens": 1,
                                                                       "output_tokens": 2}}})
        self.assertIn("completed", [event["type"] for event in done])

    def test_non_stream_chat(self):
        events = non_stream_events("openai-chat", {
            "choices": [{"message": {"content": "hello",
                                     "tool_calls": [{"id": "c", "type": "function",
                                                     "function": {"name": "f",
                                                                  "arguments": "{}"}}]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
        kinds = [event["type"] for event in events]
        self.assertIn("delta", kinds)
        self.assertIn("tool_call_delta", kinds)
        self.assertIn("usage", kinds)

    def test_non_stream_anthropic(self):
        events = non_stream_events("anthropic-messages", {
            "content": [{"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "t", "name": "f", "input": {"a": 1}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1}})
        kinds = [event["type"] for event in events]
        self.assertIn("delta", kinds)
        self.assertIn("tool_call_delta", kinds)
        self.assertEqual(events[-1]["type"], "completed")


def sse_body(chunks: list[dict]) -> bytes:
    return ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n").encode()


class ServiceTests(unittest.TestCase):
    def build(self, handler, providers, **kwargs):
        root = tempfile.mkdtemp()
        store = SettingsStore(root=Path(root))
        store.update({"apiFallback": settings_doc(providers, **kwargs)})
        transport = httpx.MockTransport(handler)
        return FallbackService(store, transport=transport), store

    def test_streams_through_the_first_candidate(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([
                                      {"choices": [{"delta": {"content": "hel"}}]},
                                      {"choices": [{"delta": {"content": "lo"}}]},
                                      {"choices": [{"delta": {}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
                                  ]))

        service, _ = self.build(handler, [provider(models=[model(sources=["*"])])])

        async def run():
            events = []
            async for event in service.run("m1", [{"role": "user", "content": "hi"}], [],
                                           {"max_tokens": 10}, reason="QUEUE_FULL"):
                events.append(event)
            return events

        events = asyncio.run(run())
        texts = "".join(event.get("text", "") for event in events if event["type"] == "delta")
        self.assertEqual(texts, "hello")
        self.assertEqual(seen["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(seen["body"]["messages"][0]["content"], "hi")
        self.assertEqual(service.last_outcome["reason"], "QUEUE_FULL")

    def test_falls_through_to_the_next_candidate(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "bad":
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [{"delta": {"content": "ok"},
                                                                  "finish_reason": "stop"}]}]))

        providers = [provider("p-00000001", models=[model("m-00000001", name="bad", priority=1)]),
                     provider("p-00000002", models=[model("m-00000002", name="good", priority=2)])]
        service, _ = self.build(handler, providers)

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5}, reason="QUEUE_FULL")]

        events = asyncio.run(run())
        self.assertEqual(calls, ["bad", "good"])
        self.assertIn("delta", [event["type"] for event in events])

    def test_all_failures_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        service, _ = self.build(handler, [provider(models=[model()])])

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5}, reason="QUEUE_FULL")]

        with self.assertRaises(FallbackError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.code, "FALLBACK_ALL_FAILED")
        self.assertTrue(service.last_outcome["failed"])

    def test_auth_failure_is_not_retryable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "nope"}})

        service, _ = self.build(handler, [provider(models=[model()])])

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5}, reason="QUEUE_FULL")]

        with self.assertRaises(FallbackError) as caught:
            asyncio.run(run())
        self.assertFalse(caught.exception.retryable)

    def test_rate_limit_moves_the_model_to_the_back(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [{"delta": {"content": "x"},
                                                                  "finish_reason": "stop"}]}]))

        providers = [provider(models=[model("m-00000001", name="a", priority=1, max_requests=1),
                                      model("m-00000002", name="b", priority=2, max_requests=1)])]
        service, _ = self.build(handler, providers)

        async def run():
            for _ in range(2):
                async for _event in service.run("m1", [{"role": "user", "content": "hi"}], [],
                                                {"max_tokens": 5}, reason="QUEUE_FULL"):
                    pass

        asyncio.run(run())
        self.assertEqual(calls, ["a", "b"])

    def test_provider_key_is_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [{"delta": {"content": "x"},
                                                                  "finish_reason": "stop"}]}]))

        service, store = self.build(handler, [provider(models=[model()])])
        store.set_fallback_api_key("p-00000001", "sk-abc")

        async def run():
            async for _event in service.run("m1", [{"role": "user", "content": "hi"}], [],
                                            {"max_tokens": 5}, reason="QUEUE_FULL"):
                pass

        asyncio.run(run())
        self.assertEqual(seen["auth"], "Bearer sk-abc")

    def test_plain_json_response_is_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "plain"},
                                                          "finish_reason": "stop"}]})

        service, _ = self.build(handler, [provider(models=[model()])])

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5}, reason="QUEUE_FULL")]

        events = asyncio.run(run())
        self.assertIn("plain", "".join(event.get("text", "") for event in events))

    def test_probe_reports_success_and_failure(self):
        def ok(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [{"delta": {"content": "p"},
                                                                  "finish_reason": "stop"}]}]))

        def bad(request):
            return httpx.Response(404, json={"error": {"message": "no model"}})

        candidate = select_candidates(
            FallbackSettings.from_document(settings_doc([provider(models=[model()])])), "m1")[0]
        good = asyncio.run(probe_provider(candidate, api_key=None,
                                          transport=httpx.MockTransport(ok)))
        self.assertTrue(good["ok"])
        failing = asyncio.run(probe_provider(candidate, api_key=None,
                                             transport=httpx.MockTransport(bad)))
        self.assertFalse(failing["ok"])
        self.assertEqual(failing["code"], "FALLBACK_REJECTED")

    def test_trigger_set_excludes_user_errors(self):
        self.assertTrue(is_trigger(MLXBarError("QUEUE_FULL", "x", 429, True)))
        self.assertTrue(is_trigger(MLXBarError("ENGINE_BUSY", "x", 429, True)))
        self.assertFalse(is_trigger(MLXBarError("MODEL_NOT_FOUND", "x", 404)))
        self.assertFalse(is_trigger(MLXBarError("INVALID_REQUEST", "x", 422)))

    def test_status_reports_usage(self):
        service, _ = self.build(lambda request: httpx.Response(500), [provider(models=[model()])])
        status = service.status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["providers"][0]["models"][0]["usedInWindow"], 0)


# ---------------------------------------------------------------------------
# integration through the real HTTP surfaces
# ---------------------------------------------------------------------------
import base64  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_management_app, make_public_app  # noqa: E402

PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)).decode()


class QueueFullWorker:
    """A worker whose queue is always full -- the primary fallback trigger."""

    def __init__(self):
        self.loaded = {"id": "local-model", "name": "Local Model", "engine": "mlx-lm"}
        self.generated = 0

    def find_loaded_model(self, requested):
        return self.loaded

    def loaded_models(self):
        return [self.loaded]

    def raise_if_queue_full(self):
        raise MLXBarError("QUEUE_FULL", "生成待ちが上限に達しています", 429, True)

    async def generate_for_model(self, model_id, messages, images, options,
                                 request_id, image_root=None):
        self.generated += 1
        yield {"type": "delta", "text": "LOCAL"}
        yield {"type": "completed", "finish_reason": "stop"}


class HealthyWorker(QueueFullWorker):
    def raise_if_queue_full(self):
        return None


def build_state(tmp: Path, worker, *, fallback=None):
    settings = SettingsStore(tmp)
    settings.data["api"]["requireToken"] = False
    if fallback is not None:
        settings.update({"apiFallback": fallback})
    database = Database(tmp / "state.sqlite3")
    database.replace_models([{
        "id": "local-model", "name": "Local Model", "engine": "mlx-vlm",
        "format": "mlx", "path": "/models/local", "modalities": ["text", "image"],
        "source": "filesystem", "provider_key": None, "confidence": 1.0,
        "size_bytes": 1 << 30, "reason": "",
    }])
    state = SimpleNamespace(settings=settings, workers=worker, database=database)
    state.model_autoload_lock = asyncio.Lock()
    state.fallback = FallbackService(settings)
    state.last_context_compression = None
    state.last_adaptive_memory = None
    state.last_rag_retrieval = None
    return state


def sse_handler(text="EXTERNAL", *, status=200, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "upstream"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_body([
                                  {"choices": [{"delta": {"role": "assistant", "content": text}}]},
                                  {"choices": [{"delta": {}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
                              ]))
    return handler


def chat_payload(**overrides):
    payload = {"model": "local-model", "stream": False,
               "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}
    payload.update(overrides)
    return payload


class OpenAISurfaceFallbackTests(unittest.TestCase):
    def test_disabled_feature_keeps_the_v402_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker())
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "QUEUE_FULL")

    def test_non_stream_falls_back_when_queue_full(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "EXTERNAL")
        # The response keeps naming the model the client asked for (the local
        # display name), exactly as a local answer would.
        self.assertEqual(body["model"], "Local Model")
        self.assertEqual(captured[0]["messages"][0]["content"], "hi")

    def test_stream_falls_back_and_keeps_sse_shape(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler("STREAMED"))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(stream=True))
        self.assertEqual(response.status_code, 200)
        lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        chunks = [json.loads(line[6:]) for line in lines[:-1]]
        text = "".join(chunk["choices"][0]["delta"].get("content") or ""
                       for chunk in chunks if chunk.get("choices"))
        self.assertEqual(text, "STREAMED")
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")

    def test_all_candidates_failing_returns_the_local_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=500))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "QUEUE_FULL")

    def test_model_not_found_never_falls_back(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions",
                                   json=chat_payload(model="does-not-exist"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(captured, [])

    def test_unassigned_model_never_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model(sources=["other"])])]))
            state.fallback.transport = httpx.MockTransport(sse_handler())
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 429)

    def test_images_go_to_a_vision_capable_model_even_with_queue_room(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(), fallback=settings_doc([
                provider(models=[model("m-00000001", name="text-only", priority=1),
                                 model("m-00000002", name="vision", priority=2, images=True)]),
            ]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
                ]}]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured[0]["model"], "vision")
        parts = captured[0]["messages"][0]["content"]
        self.assertTrue(any(part.get("type") == "image_url" for part in parts))

    def test_images_without_a_vision_model_stay_local(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            worker = HealthyWorker()
            state = build_state(Path(root), worker, fallback=settings_doc([
                provider(models=[model(images=False)])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
                ]}]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [])
        self.assertEqual(worker.generated, 1)

    def test_anthropic_protocol_is_used_when_configured(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(), fallback=settings_doc([
                provider(protocol="anthropic-messages", models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(captured[0]["max_tokens"], 32)
        self.assertIn("messages", captured[0])

    def test_responses_protocol_is_used_when_configured(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(), fallback=settings_doc([
                provider(protocol="openai-responses", models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            client.post("/v1/chat/completions", json=chat_payload())
        self.assertIn("input", captured[0])
        self.assertEqual(captured[0]["input"][0]["content"][0]["text"], "hi")


class AnthropicSurfaceFallbackTests(unittest.TestCase):
    def payload(self, **overrides):
        body = {"model": "local-model", "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}]}
        body.update(overrides)
        return body

    def test_messages_fall_back_when_queue_full(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler("ANTHRO", capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages", json=self.payload(),
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["content"][0]["text"], "ANTHRO")

    def test_messages_stream_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler("S"))
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages", json=self.payload(stream=True),
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("message_start", response.text)
        self.assertIn("message_stop", response.text)
        self.assertIn("S", response.text)

    def test_messages_disabled_keeps_the_original_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker())
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages", json=self.payload(),
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "overloaded_error")


class ManagementApiTests(unittest.TestCase):
    def client(self, root):
        state = build_state(Path(root), HealthyWorker())
        state.jobs = SimpleNamespace()
        return TestClient(make_management_app(state)), state

    def test_crud_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            client, state = self.client(root)
            created = client.post("/api/v1/fallback/providers",
                                  json={"name": "OpenAI", "baseURL": "https://api.openai.com/v1"})
            self.assertEqual(created.status_code, 201)
            provider_id = created.json()["providerId"]
            added = client.post(f"/api/v1/fallback/providers/{provider_id}/models",
                                json={"model": "gpt-4.1-mini", "sourceModels": ["*"],
                                      "supportsImages": True})
            self.assertEqual(added.status_code, 201)
            model_id = added.json()["modelId"]
            updated = client.put(f"/api/v1/fallback/providers/{provider_id}/models/{model_id}",
                                 json={"priority": 1, "maxRequests": 5})
            self.assertEqual(updated.status_code, 200)
            document = updated.json()["apiFallback"]
            self.assertEqual(document["providers"][0]["models"][0]["priority"], 1)
            self.assertEqual(document["providers"][0]["models"][0]["maxRequests"], 5)
            key = client.put(f"/api/v1/fallback/providers/{provider_id}/api-key",
                             json={"apiKey": "sk-test"})
            self.assertTrue(key.json()["apiFallback"]["providers"][0]["apiKeyConfigured"])
            self.assertNotIn("sk-test", json.dumps(key.json()))
            removed = client.delete(f"/api/v1/fallback/providers/{provider_id}")
            self.assertEqual(removed.json()["apiFallback"]["providers"], [])
            self.assertFalse((Path(root) / "control" / f"fallback-{provider_id}.key").exists())

    def test_invalid_update_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            client, _ = self.client(root)
            response = client.put("/api/v1/fallback", json={"maxAttempts": 0})
        self.assertEqual(response.status_code, 422)

    def test_unknown_provider_is_404(self):
        with tempfile.TemporaryDirectory() as root:
            client, _ = self.client(root)
            response = client.delete("/api/v1/fallback/providers/p-deadbeef")
        self.assertEqual(response.status_code, 404)

    def test_status_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            client, _ = self.client(root)
            response = client.get("/api/v1/fallback/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])


class CliParityTests(unittest.TestCase):
    """The CLI must write exactly the shape the management API validates."""

    def run_cli(self, **fields):
        from mlxbar.cli import execute
        client = Mock()
        client.request.return_value = Mock(json=Mock(return_value={}))
        execute(argparse.Namespace(global_json=False, **fields), client)
        return client

    def test_set_common_options(self):
        client = self.run_cli(command="fallback", action="set", enabled="true",
                              cooldown_seconds=30, max_attempts=5,
                              forward_images_immediately="false")
        client.request.assert_called_once_with(
            "PUT", "/api/v1/fallback",
            {"enabled": True, "cooldownSeconds": 30, "maxAttempts": 5,
             "forwardImagesImmediately": False})

    def test_add_provider(self):
        client = self.run_cli(command="fallback", action="add-provider", name="OpenAI",
                              protocol="openai-chat", base_url="https://api.openai.com/v1",
                              timeout_seconds=120)
        client.request.assert_called_once_with(
            "POST", "/api/v1/fallback/providers",
            {"name": "OpenAI", "protocol": "openai-chat",
             "baseURL": "https://api.openai.com/v1", "timeoutSeconds": 120})

    def test_add_model_defaults_to_all_models(self):
        client = self.run_cli(command="fallback", action="add-model", provider_id="p-00000001",
                              model="gpt", label="", priority=10, source_model=None,
                              supports_images="false", max_requests=0,
                              rate_window_seconds=60, max_output_tokens=0,
                              max_context_tokens=0, image_detail="auto")
        body = client.request.call_args[0][2]
        self.assertEqual(body["sourceModels"], ["*"])
        self.assertFalse(body["sourceModelsExplicit"])

    def test_add_model_with_explicit_sources(self):
        client = self.run_cli(command="fallback", action="add-model", provider_id="p-00000001",
                              model="gpt", label="", priority=10,
                              source_model=["m1", "m2"], supports_images="true",
                              max_requests=10, rate_window_seconds=60,
                              max_output_tokens=0, max_context_tokens=0, image_detail="auto")
        body = client.request.call_args[0][2]
        self.assertEqual(body["sourceModels"], ["m1", "m2"])
        self.assertTrue(body["sourceModelsExplicit"])
        self.assertTrue(body["supportsImages"])

    def test_set_rejects_an_empty_patch(self):
        with self.assertRaises(ValueError):
            self.run_cli(command="fallback", action="set", enabled=None,
                         cooldown_seconds=None, max_attempts=None,
                         forward_images_immediately=None)

    def test_set_rejects_out_of_range_attempts(self):
        with self.assertRaises(ValueError):
            self.run_cli(command="fallback", action="set", enabled=None,
                         cooldown_seconds=None, max_attempts=0,
                         forward_images_immediately=None)

    def test_secret_key_write_goes_through_the_provider_route(self):
        client = self.run_cli(command="secrets", action="set-fallback-key",
                              provider_id="p-00000001", token="sk-x")
        client.request.assert_called_once_with(
            "PUT", "/api/v1/fallback/providers/p-00000001/api-key", {"apiKey": "sk-x"})


class EarlyLocalErrorTests(unittest.TestCase):
    """A local failure while *resolving* the model must still fall back.

    Found by the live run (`scripts/verify-fallback-runtime.py` 2-2): the
    capacity check below reset `fallback_reason` to None, so a request refused
    with MODEL_NOT_LOADED during resolution reached the external provider in
    the log but the local error was raised anyway.
    """

    class RefusingWorker(HealthyWorker):
        def __init__(self):
            super().__init__()
            # Nothing resident: `_ensure_requested_model` resolves the catalog
            # row and takes the (failing) autoload path. `loaded` must be None
            # too -- the generic-alias shortcut would otherwise answer with the
            # resident model.
            self.loaded = None

        def find_loaded_model(self, requested):
            return None

        async def load_for_api(self, model):
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409, True)

        async def load(self, model, engine=None, pin=True):
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409, True)

    def test_resolution_failure_falls_back(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), self.RefusingWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.settings.data["models"]["autoLoadOnAPIRequest"] = True
            state.fallback.transport = httpx.MockTransport(sse_handler("EARLY", capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "EARLY")
        self.assertEqual(len(captured), 1)

    def test_resolution_failure_without_fallback_keeps_the_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), self.RefusingWorker())
            state.settings.data["models"]["autoLoadOnAPIRequest"] = True
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "MODEL_NOT_LOADED")

    def test_anthropic_resolution_failure_falls_back(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), self.RefusingWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.settings.data["models"]["autoLoadOnAPIRequest"] = True
            state.fallback.transport = httpx.MockTransport(sse_handler("EARLY", capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages",
                                   json={"model": "local-model", "max_tokens": 16,
                                         "messages": [{"role": "user", "content": "hi"}]},
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"][0]["text"], "EARLY")


class ImageForwardFailureTests(unittest.TestCase):
    """The image fast path has no local error to report when it fails.

    With images attached and a healthy local model, the fallback is chosen
    because an external vision model is preferred -- not because MLXBar
    refused. If every candidate then fails, the external error is the only
    honest answer (previously the handler dereferenced a None local error).
    """

    def test_all_candidates_failing_reports_the_external_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(), fallback=settings_doc([
                provider(models=[model(images=True)])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=500))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "what"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
                ]}]))
        self.assertIn(response.status_code, (502, 503))
        self.assertEqual(response.json()["error"]["code"], "FALLBACK_ALL_FAILED")

    def test_streaming_image_failure_reports_an_error_event(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(), fallback=settings_doc([
                provider(models=[model(images=True)])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=500))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(
                stream=True,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "what"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
                ]}]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("FALLBACK_ALL_FAILED", response.text)
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))


class UpstreamErrorAsSSETests(unittest.TestCase):
    """An upstream failure delivered as an SSE event must never become a
    silent empty success (review finding 2).

    Anthropic sends `event: error`, Responses sends `response.failed`, and some
    OpenAI-compatible servers send a bare `{"error": ...}` frame. Before any
    content these must raise so the next candidate is tried; after content they
    must reach the client as an error event.
    """

    def service(self, handler, providers=None):
        root = tempfile.mkdtemp()
        store = SettingsStore(root=Path(root))
        store.update({"apiFallback": settings_doc(providers or [provider(models=[model()])])})
        return FallbackService(store, transport=httpx.MockTransport(handler))

    def run_events(self, service):
        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5},
                reason="QUEUE_FULL")]
        return asyncio.run(run())

    def test_anthropic_error_event_falls_through_to_the_next_candidate(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "bad":
                payload = ("event: error\ndata: " + json.dumps(
                    {"type": "error", "error": {"type": "overloaded_error",
                                                "message": "busy"}}) + "\n\n").encode()
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=payload)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [
                                      {"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

        providers = [provider("p-00000001", protocol="anthropic-messages",
                              models=[model("m-00000001", name="bad", priority=1)]),
                     provider("p-00000002", models=[model("m-00000002", name="good", priority=2)])]
        events = self.run_events(self.service(handler, providers))
        self.assertEqual(calls, ["bad", "good"])
        self.assertIn("delta", [event["type"] for event in events])

    def test_responses_failed_event_falls_through(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "bad":
                payload = ("data: " + json.dumps({"type": "response.failed", "response": {
                    "status": "failed", "error": {"message": "nope"}}}) + "\n\n").encode()
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=payload)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [
                                      {"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

        providers = [provider("p-00000001", protocol="openai-responses",
                              models=[model("m-00000001", name="bad", priority=1)]),
                     provider("p-00000002", models=[model("m-00000002", name="good", priority=2)])]
        events = self.run_events(self.service(handler, providers))
        self.assertEqual(calls, ["bad", "good"])

    def test_all_candidates_erroring_raises_instead_of_returning_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = ("data: " + json.dumps({"error": {"message": "down"}}) + "\n\n").encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=payload)

        with self.assertRaises(FallbackError):
            self.run_events(self.service(handler))

    def test_error_after_content_is_reported_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            # No [DONE]: the failure interrupts the stream (a [DONE]-terminated
            # body is a completed response by definition).
            payload = ("data: " + json.dumps({"choices": [{"delta": {"content": "part"}}]})
                       + "\n\ndata: " + json.dumps({"error": {"message": "cut"}}) + "\n\n").encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=payload)

        events = self.run_events(self.service(handler))
        self.assertEqual(len(calls), 1)
        self.assertIn("error", [event["type"] for event in events])
        self.assertIn("delta", [event["type"] for event in events])


class AnthropicAllFailedTests(unittest.TestCase):
    """Review finding 1: the Anthropic surface must not answer with a raw 500."""

    def test_all_candidates_failing_reports_the_local_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(),
                                fallback=settings_doc([provider(models=[model()])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=401))
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages",
                                   json={"model": "local-model", "max_tokens": 16,
                                         "messages": [{"role": "user", "content": "hi"}]},
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "overloaded_error")

    def test_image_fast_path_failure_reports_the_external_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(), fallback=settings_doc([
                provider(models=[model(images=True)])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=500))
            client = TestClient(make_public_app(state))
            response = client.post("/anthropic/v1/messages",
                                   json={"model": "local-model", "max_tokens": 16,
                                         "messages": [{"role": "user", "content": [
                                             {"type": "text", "text": "what"},
                                             {"type": "image", "source": {
                                                 "type": "base64", "media_type": "image/png",
                                                 "data": PNG_1PX}}]}]},
                                   headers={"anthropic-version": "2023-06-01"})
        self.assertIn(response.status_code, (502, 503))
        self.assertEqual(response.json()["error"]["type"], "api_error")


class ReviewFindingsTests(unittest.TestCase):
    """Regressions for the independent review of v5.0.0 (DESIGN_v5.0.0.md §8)."""

    # -- H-1: display-name source models must match -----------------------
    def test_source_models_match_the_display_name(self):
        captured = []
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(), fallback=settings_doc([
                provider(models=[model(sources=["Local Model"])])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(capture=captured))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)

    def test_source_models_match_the_catalog_id(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), QueueFullWorker(), fallback=settings_doc([
                provider(models=[model(sources=["local-model"])])]))
            state.fallback.transport = httpx.MockTransport(sse_handler())
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload())
        self.assertEqual(response.status_code, 200)

    # -- M-1: an empty stream is not an empty answer ----------------------
    def test_empty_stream_falls_through_to_the_next_candidate(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body["model"])
            if body["model"] == "empty":
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=b"data: [DONE]\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=sse_body([{"choices": [
                                      {"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

        providers = [provider("p-00000001", models=[model("m-00000001", name="empty", priority=1)]),
                     provider("p-00000002", models=[model("m-00000002", name="good", priority=2)])]
        root = tempfile.mkdtemp()
        store = SettingsStore(root=Path(root))
        store.update({"apiFallback": settings_doc(providers)})
        service = FallbackService(store, transport=httpx.MockTransport(handler))

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5},
                reason="QUEUE_FULL")]

        events = asyncio.run(run())
        self.assertEqual(calls, ["empty", "good"])
        self.assertIn("delta", [event["type"] for event in events])

    def test_empty_stream_everywhere_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b": comment only\n\n")

        root = tempfile.mkdtemp()
        store = SettingsStore(root=Path(root))
        store.update({"apiFallback": settings_doc([provider(models=[model()])])})
        service = FallbackService(store, transport=httpx.MockTransport(handler))

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5},
                reason="QUEUE_FULL")]

        with self.assertRaises(FallbackError):
            asyncio.run(run())

    # -- M-2: the tool-call argument buffer is bounded --------------------
    def test_tool_argument_buffer_is_bounded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            chunk = {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": "x" * 600_000}}]}}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=("data: " + json.dumps(chunk) + "\n\n").encode() * 4)

        root = tempfile.mkdtemp()
        store = SettingsStore(root=Path(root))
        store.update({"apiFallback": settings_doc([provider(models=[model()])])})
        service = FallbackService(store, transport=httpx.MockTransport(handler))

        async def run():
            return [event async for event in service.run(
                "m1", [{"role": "user", "content": "hi"}], [], {"max_tokens": 5},
                reason="QUEUE_FULL")]

        with self.assertRaises(FallbackError) as caught:
            asyncio.run(run())
        # `run` reports the aggregate; the per-candidate reason is recorded.
        self.assertEqual(caught.exception.code, "FALLBACK_ALL_FAILED")
        self.assertEqual(service.last_outcome["attempts"][0]["code"], "FALLBACK_PROTOCOL_ERROR")

    # -- M-4: one broken model must not delete every provider -------------
    def test_one_broken_model_keeps_the_providers(self):
        with tempfile.TemporaryDirectory() as root:
            store = SettingsStore(root=Path(root))
            store.update({"apiFallback": settings_doc([provider(models=[model()])])})
            path = Path(root) / "config.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["apiFallback"]["providers"][0]["models"][0]["priority"] = "high"
            path.write_text(json.dumps(document), encoding="utf-8")
            reloaded = SettingsStore(root=Path(root))
        fallback = reloaded.data["apiFallback"]
        self.assertTrue(fallback["enabled"])
        self.assertEqual(len(fallback["providers"]), 1)
        self.assertEqual(fallback["providers"][0]["models"], [])

    # -- M-5: text/image order survives the OpenAI-chat translation -------
    def test_ordered_parts_are_preserved(self):
        content = [{"type": "text", "text": "before"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                   {"type": "text", "text": "after"}]
        body = openai_chat_body({"model": "x"},
                                [{"role": "user", "content": content}], [], {}, False)
        parts = body["messages"][0]["content"]
        self.assertEqual([part["type"] for part in parts],
                         ["text", "image_url", "text"])
        self.assertEqual(parts[2]["text"], "after")

    # -- L-2: a stale whole-array PUT cannot resurrect a provider ---------
    def test_put_cannot_resurrect_a_deleted_provider(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker())
            client = TestClient(make_management_app(state))
            created = client.post("/api/v1/fallback/providers",
                                  json={"name": "A", "baseURL": "https://a"}).json()
            provider_id = created["providerId"]
            stale = created["apiFallback"]
            client.delete(f"/api/v1/fallback/providers/{provider_id}")
            response = client.put("/api/v1/fallback", json={"providers": stale["providers"]})
        self.assertEqual(response.status_code, 409)
        # The management app uses FastAPI's `detail` envelope (the fallback
        # router raises HTTPException directly).
        detail = response.json().get("detail") or {}
        self.assertEqual(detail.get("code"), "SETTINGS_CONFLICT")
        self.assertEqual(detail.get("providers"), [provider_id])

    # -- L-4: the image fast path reports a server error, not a bad request
    def test_image_fast_path_failure_is_a_server_error(self):
        with tempfile.TemporaryDirectory() as root:
            state = build_state(Path(root), HealthyWorker(), fallback=settings_doc([
                provider(models=[model(images=True)])]))
            state.fallback.transport = httpx.MockTransport(sse_handler(status=500))
            client = TestClient(make_public_app(state))
            response = client.post("/v1/chat/completions", json=chat_payload(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "what"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX}"}},
                ]}]))
        self.assertEqual(response.json()["error"]["type"], "server_error")
