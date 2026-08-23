"""Reuse of a prompt cache across turns, branches and interruptions (v1.6.0).

The behaviour under test is what happens *between* generations, so most of it
cannot be reached through a real model: it needs a cache that advances, a turn
that is cut short, and an architecture that cannot roll its cache back. Those
are simulated here.

What the fakes do and do not prove is worth stating, because v1.5.1 shipped a
defect that its own tests could not see. A fake `mlx.core` exercises the shape
of the state tree -- lists, tuples, `None` holes, round-tripping through the
manifest -- and it exercises every branch of the reuse policy. It does not
prove anything about MLX's own array semantics, and a test that needs those
belongs on real hardware in TEST_PLAN_v1.6.0.md instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from common import cache_state  # noqa: E402
from mlx_vlm_worker.prompt_cache import CheckpointStore, build_guarded_state  # noqa: E402


# --------------------------------------------------------------- fake runtime

class FakeArray:
    def __init__(self, data, dtype=None):
        source = data.data if isinstance(data, FakeArray) else data
        self.data = list(source)
        self.dtype = dtype or (data.dtype if isinstance(data, FakeArray) else "bfloat16")

    @property
    def nbytes(self) -> int:
        return len(self.data) * 2

    def __eq__(self, other):
        return isinstance(other, FakeArray) and self.data == other.data

    def __repr__(self):
        return f"FakeArray({self.data})"


def fake_mlx(tmp_path: Path | None = None) -> ModuleType:
    module = ModuleType("mlx")
    core = ModuleType("mlx.core")
    core.array = FakeArray
    core.contiguous = lambda value: value
    core.eval = lambda *args: None

    def save_safetensors(path, arrays):
        # mlx appends the extension when the path does not already end in it,
        # so a caller that writes to "<name>.safetensors.tmp" gets a file it
        # never named. Mirrored here because the difference is the whole bug:
        # a fake that writes exactly where it is told cannot show it.
        if not str(path).endswith(".safetensors"):
            path = str(path) + ".safetensors"
        payload = {name: value.data for name, value in arrays.items()}
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def load(path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return {name: FakeArray(values) for name, values in payload.items()}

    core.save_safetensors = save_safetensors
    core.load = load
    module.core = core
    return module


@pytest.fixture()
def mlx_stub(monkeypatch):
    module = fake_mlx()
    monkeypatch.setitem(sys.modules, "mlx", module)
    monkeypatch.setitem(sys.modules, "mlx.core", module.core)
    return module


class TrimmableCache:
    """A plain KV cache: it can drop trailing tokens where it stands."""

    def __init__(self, offset: int = 0):
        self.offset = offset
        self._state = [FakeArray([1, 2, 3])]

    def trim(self, count: int) -> int:
        self.offset -= count
        return count

    def is_trimmable(self) -> bool:
        return True

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value


class EmptyKVCache:
    """A plain KV cache before its first token, exactly as mlx-lm builds one.

    Its `state` getter reads `self.keys.shape` and therefore raises while `keys`
    is still None. The capability probe runs on a cache in precisely this state.
    """

    def __init__(self):
        self.keys = None
        self.values = None
        self.offset = 0

    @property
    def state(self):
        if self.offset == self.keys.shape[2]:
            return self.keys, self.values
        return self.keys, self.values

    @state.setter
    def state(self, value):
        self.keys, self.values = value

    def is_trimmable(self) -> bool:
        return True

    def trim(self, count: int) -> int:
        return count


class RecurrentCache:
    """A linear-attention cache: capturable, but with nothing to trim."""

    def __init__(self):
        self._state = [FakeArray([9, 9]), None]

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value


class BasePromptCacheState:
    """Stands in for the runtime's own PromptCacheState."""

    def __init__(self):
        self.cache = None
        self.token_ids = None

    def find_prefix_length(self, new_ids: list) -> int:
        if self.token_ids is None:
            return 0
        limit = min(len(self.token_ids), len(new_ids))
        for index in range(limit):
            if self.token_ids[index] != new_ids[index]:
                return index
        return limit

    def update(self, token_ids: list, kv_cache: list) -> None:
        self.token_ids = list(token_ids)
        self.cache = kv_cache


class Controller:
    def __init__(self, restore=None):
        self.prompts: list[list[int]] = []
        self._restore = restore

    def observe_prompt(self, token_ids):
        self.prompts.append(list(token_ids))

    def restore_for(self, token_ids):
        return self._restore(token_ids) if self._restore else None


# ------------------------------------------------------------- capability

def test_a_cache_without_trim_is_capturable_but_not_trimmable():
    hybrid = [TrimmableCache(), RecurrentCache()]
    assert cache_state.can_trim(hybrid) is False
    assert cache_state.can_capture(hybrid) is True
    assert cache_state.rollback_capability(hybrid) == cache_state.ROLLBACK_CHECKPOINT


def test_an_empty_kv_cache_is_still_capturable():
    """The probe runs before the first token, and must not read a value.

    Reading `state` on an empty KVCache raises, and `hasattr` reports that as
    "no such attribute". Judging the instance therefore reported every hybrid
    -- a recurrent stack plus plain attention layers -- as incapable of the one
    rollback it can actually do, which switched snapshots off for exactly the
    architectures they exist for.
    """
    assert cache_state.can_capture([EmptyKVCache()]) is True
    hybrid = [EmptyKVCache(), RecurrentCache()]
    assert cache_state.can_capture(hybrid) is True
    assert cache_state.rollback_capability(hybrid) == cache_state.ROLLBACK_CHECKPOINT


def test_a_type_without_a_state_setter_is_not_capturable():
    class ReadOnly:
        @property
        def state(self):
            return []

    class Plain:
        state = []

    assert cache_state.can_capture([ReadOnly()]) is False
    assert cache_state.can_capture([Plain()]) is False


def test_a_plain_kv_cache_reports_trim():
    assert cache_state.rollback_capability([TrimmableCache()]) == cache_state.ROLLBACK_TRIM


def test_a_composite_cache_is_only_as_capable_as_its_weakest_part():
    composite = SimpleNamespace(caches=[TrimmableCache(), RecurrentCache()])
    assert cache_state.can_trim([composite]) is False


def test_is_trimmable_false_overrides_a_present_trim_method():
    class Declining(TrimmableCache):
        def is_trimmable(self) -> bool:
            return False

    assert cache_state.can_trim([Declining()]) is False


def test_cached_length_is_none_when_nothing_reports_an_offset():
    assert cache_state.cached_length([RecurrentCache()]) is None
    assert cache_state.cached_length([TrimmableCache(offset=42)]) == 42


# ---------------------------------------------------------------- budget

HYBRID_CONFIG = {
    "text_config": {
        "num_hidden_layers": 64,
        "layer_types": ["linear_attention"] * 48 + ["full_attention"] * 16,
        "num_key_value_heads": 4,
        "head_dim": 256,
        "dtype": "bfloat16",
        "mamba_ssm_dtype": "float32",
        "linear_num_value_heads": 48,
        "linear_num_key_heads": 16,
        "linear_key_head_dim": 128,
        "linear_value_head_dim": 128,
        "linear_conv_kernel_dim": 4,
    }
}


def test_budget_counts_only_full_attention_layers_per_token():
    budget = cache_state.model_cache_budget(HYBRID_CONFIG)
    assert budget["known"] is True
    assert budget["fullAttentionLayers"] == 16
    assert budget["recurrentLayers"] == 48
    # keys and values, 16 layers, 4 kv heads, 256 wide, 2 bytes each
    assert budget["perTokenBytes"] == 16 * 2 * 4 * 256 * 2
    assert budget["fixedBytes"] > 0


def test_budget_falls_back_to_the_attention_interval_when_layer_types_are_absent():
    config = {"text_config": dict(HYBRID_CONFIG["text_config"])}
    config["text_config"].pop("layer_types")
    config["text_config"]["full_attention_interval"] = 4
    assert cache_state.model_cache_budget(config)["fullAttentionLayers"] == 16


def test_a_dense_model_counts_every_layer():
    config = {"num_hidden_layers": 32, "num_key_value_heads": 8, "head_dim": 128,
              "dtype": "bfloat16"}
    budget = cache_state.model_cache_budget(config)
    assert budget["fullAttentionLayers"] == 32
    assert budget["recurrentLayers"] == 0


def test_an_unreadable_config_reports_unknown_rather_than_a_guess():
    assert cache_state.model_cache_budget({})["known"] is False
    assert cache_state.model_cache_budget({"num_hidden_layers": 4})["known"] is False


def test_affordable_tokens_is_zero_when_the_fixed_part_alone_overflows():
    budget = cache_state.model_cache_budget(HYBRID_CONFIG)
    assert cache_state.affordable_tokens(budget, 1024) == 0
    assert cache_state.affordable_tokens(budget, 8 << 30) > 100_000


# ------------------------------------------------------- capture/restore

def test_capture_and_restore_round_trip_including_none_holes(mlx_stub):
    original = [TrimmableCache(), RecurrentCache()]
    payload = cache_state.capture(original)
    assert payload is not None

    target = [TrimmableCache(), RecurrentCache()]
    target[0].state = [FakeArray([0])]
    target[1].state = [FakeArray([0]), FakeArray([0])]
    assert cache_state.restore(target, payload) is True
    assert target[0].state[0].data == [1, 2, 3]
    assert target[1].state[1] is None


def test_a_capture_does_not_follow_the_live_cache_forward(mlx_stub):
    live = [TrimmableCache()]
    payload = cache_state.capture(live)
    live[0].state[0].data.append(99)
    restored = [TrimmableCache()]
    cache_state.restore(restored, payload)
    assert restored[0].state[0].data == [1, 2, 3]


def test_restore_refuses_a_payload_of_the_wrong_shape(mlx_stub):
    payload = cache_state.capture([TrimmableCache()])
    assert cache_state.restore([TrimmableCache(), RecurrentCache()], payload) is False


def test_flatten_and_unflatten_preserve_structure(mlx_stub):
    payload = cache_state.capture([TrimmableCache(), RecurrentCache()])
    arrays, manifest = cache_state.flatten(payload)
    rebuilt = cache_state.unflatten(arrays, manifest)
    assert rebuilt[1]["state"][1] is None
    assert rebuilt[0]["state"][0].data == [1, 2, 3]


def test_flatten_refuses_a_node_it_cannot_restore_faithfully(mlx_stub):
    payload = [{"state": [object()], "meta": None}]
    with pytest.raises(TypeError):
        cache_state.flatten(payload)


# ------------------------------------------------------- the reuse policy

def guarded(controller, token_ids, cache):
    state = build_guarded_state(BasePromptCacheState, controller)
    state.token_ids = list(token_ids)
    state.cache = cache
    state.begin_request()
    return state


def test_a_continuation_reuses_everything_it_holds():
    controller = Controller()
    state = guarded(controller, [1, 2, 3], [RecurrentCache()])
    assert state.find_prefix_length([1, 2, 3, 4, 5]) == 3
    assert state.last_probe["action"] == "reuse"


def test_a_branch_on_a_trimmable_cache_is_left_to_the_runtime():
    controller = Controller()
    state = guarded(controller, [1, 2, 3, 4], [TrimmableCache(offset=4)])
    assert state.find_prefix_length([1, 2, 9]) == 2
    assert state.last_probe["action"] == "trim"


def test_a_branch_on_a_cache_that_cannot_trim_is_refused_before_the_runtime_tries():
    controller = Controller()
    state = guarded(controller, [1, 2, 3, 4], [RecurrentCache()])
    assert state.find_prefix_length([1, 2, 9]) == 0
    assert state.last_probe["action"] == "cold"
    assert state.last_probe["reason"] == cache_state.COLD_REUSE_UNSUPPORTED


def test_a_branch_restores_a_snapshot_when_one_matches():
    snapshot = [RecurrentCache()]
    controller = Controller(restore=lambda ids: (snapshot, [1, 2], "disk"))
    state = guarded(controller, [1, 2, 3, 4], [RecurrentCache()])
    assert state.find_prefix_length([1, 2, 9, 9]) == 2
    assert list(state.cache) == snapshot
    assert state.token_ids == [1, 2]
    assert state.last_probe["restoredFrom"] == "disk"


def test_a_snapshot_reaches_a_caller_that_read_the_cache_first():
    """Ordering independence: the snapshot moves into the list, not over it.

    mlx-vlm 0.6.15 reads `.cache` once to test it against None and again after
    asking for the prefix length; a future version may read it only once, and
    before asking. Restoring into the retained list rather than replacing it
    means the reference a caller already holds becomes the restored cache, so
    the length that is returned and the cache that is used still describe the
    same tokens either way.
    """
    snapshot = [RecurrentCache()]
    controller = Controller(restore=lambda ids: (snapshot, [1, 2], "memory"))
    original = [RecurrentCache()]
    state = guarded(controller, [1, 2, 3, 4], original)
    early = state.cache  # the caller reads it before asking
    assert state.find_prefix_length([1, 2, 9, 9]) == 2
    assert list(early) == snapshot
    assert early is state.cache


def test_a_refused_branch_releases_the_cache_but_leaves_it_iterable():
    """The retained cache is emptied, never replaced with None.

    Emptying releases the components before a replacement is built, which is the
    point: on this path the cache is unusable either way. Handing back None
    instead would break the caller, which has already passed its own
    `cache is not None` test and goes on to compute a drop amount by iterating
    whatever it is given.
    """
    controller = Controller()
    original = [RecurrentCache()]
    state = guarded(controller, [1, 2, 3, 4], original)
    assert state.find_prefix_length([1, 2, 9]) == 0
    assert state.cache == []
    assert original == []
    assert state.token_ids is None


def test_a_continuation_is_refused_when_the_cache_sits_ahead_of_its_labels():
    """The invariant that keeps `trim()` unreachable.

    A cache whose offset is longer than the prefix being claimed leaves the
    runtime with tokens to drop, and it computes that amount from the cache
    rather than from the length it was handed. On a recurrent component the drop
    is not merely wrong, it does not exist -- so the length is refused instead.
    """
    controller = Controller()
    state = guarded(controller, [1, 2, 3], [TrimmableCache(offset=5), RecurrentCache()])
    assert state.find_prefix_length([1, 2, 3, 4]) == 0
    assert state.last_probe["reason"] == cache_state.COLD_REUSE_UNSUPPORTED


def test_a_continuation_is_allowed_when_the_cache_agrees_with_its_labels():
    controller = Controller()
    state = guarded(controller, [1, 2, 3], [TrimmableCache(offset=3), RecurrentCache()])
    assert state.find_prefix_length([1, 2, 3, 4]) == 3
    assert state.last_probe["action"] == "reuse"


def test_no_answer_ever_leaves_the_runtime_with_tokens_to_drop():
    """Replays what mlx-vlm does with the answer, for every shape of request.

    `dispatch._prefix_cache_trim_amount` takes the largest offset any component
    reports and subtracts the prefix length; anything left over becomes a
    `trim()` call on every component. A recurrent component has no `trim`, so
    the only safe answers are ones that leave nothing over.
    """
    def runtime_would_trim(state, new_ids) -> bool:
        if state.cache is None:                      # dispatch's own guard
            return False
        prefix_len = state.find_prefix_length(new_ids)
        kv_cache = state.cache
        cached_len = max((int(getattr(c, "offset", 0) or 0) for c in kv_cache), default=0)
        n_drop = max(0, cached_len - prefix_len)
        return bool(0 < prefix_len < len(new_ids) and n_drop)

    for held_ids, offset, prompt in (
        ([1, 2, 3], 3, [1, 2, 3, 4]),            # continuation, cache in step
        ([1, 2, 3], 5, [1, 2, 3, 4]),            # continuation, cache ahead
        ([1, 2, 3, 4], 4, [1, 2, 9]),            # branch
        ([1, 2, 3, 4], 6, [1, 2, 9]),            # branch, cache ahead
        ([1, 2, 3], 3, [9, 9, 9]),               # nothing shared
    ):
        state = guarded(Controller(), held_ids,
                        [TrimmableCache(offset=offset), RecurrentCache()])
        assert runtime_would_trim(state, prompt) is False, (held_ids, offset, prompt)


def test_an_emptied_cache_is_never_claimed_as_a_prefix():
    """The state left behind by a refused branch must not be reused.

    The runtime's own guard is `cache is not None`, so an emptied list passes
    it. Answering with a length then makes the runtime drop that many tokens
    from the prompt and prefill the rest into an empty cache -- a wrong answer,
    not a slow one.
    """
    controller = Controller()
    state = guarded(controller, [1, 2, 3, 4], [RecurrentCache()])
    assert state.find_prefix_length([1, 2, 9]) == 0      # refuses, empties
    state.token_ids = [1, 2, 3, 4]                        # as if labels survived
    assert state.find_prefix_length([1, 2, 3, 4, 5]) == 0
    assert state.last_probe["action"] == "cold"


def test_the_first_request_is_reported_as_such():
    controller = Controller()
    state = build_guarded_state(BasePromptCacheState, controller)
    state.cache = [RecurrentCache()]
    state.begin_request()
    assert state.find_prefix_length([1, 2, 3]) == 0
    assert state.last_probe["reason"] == cache_state.COLD_FIRST_REQUEST


def test_the_prompt_is_reported_to_the_controller_for_later_relabelling():
    controller = Controller()
    state = guarded(controller, [1, 2], [RecurrentCache()])
    state.find_prefix_length([1, 2, 3])
    assert controller.prompts == [[1, 2, 3]]


# ------------------------------------------------------- checkpoint store

def budgeted_store(tmp_path, mlx_stub, max_bytes=8 << 30, write_budget=8 << 30):
    return CheckpointStore(root=tmp_path, namespace="ns",
                           budget=cache_state.model_cache_budget(HYBRID_CONFIG),
                           max_bytes=max_bytes, keep_generations=2,
                           write_budget_bytes=write_budget)


def test_a_store_refuses_to_write_what_its_limit_cannot_hold(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub, max_bytes=1 << 20)
    store.remember(list(range(1000)), [TrimmableCache()])
    assert store.persist(list(range(1000))) == cache_state.COLD_BUDGET_INSUFFICIENT
    assert store.disabled_reason == cache_state.COLD_BUDGET_INSUFFICIENT


def test_a_persisted_snapshot_lands_under_the_name_the_index_records(tmp_path, mlx_stub):
    """mlx renames a path that does not end in `.safetensors`.

    Writing to "<digest>.safetensors.tmp" produced "<digest>.safetensors.tmp
    .safetensors" -- a file the chmod, the rename and the eviction all miss. The
    snapshot was never readable and the leftover was never reclaimed, at roughly
    200 MB each.
    """
    store = budgeted_store(tmp_path, mlx_stub)
    ids = list(range(1024))
    assert store.remember(ids, [RecurrentCache()]) is True
    assert store.persist(ids) is None
    files = sorted(item.name for item in store.directory.iterdir())
    assert any(name.endswith(".safetensors") and ".tmp" not in name for name in files), files
    assert not [name for name in files if ".tmp" in name], files
    assert store.restore_best(ids + [9], lambda: [RecurrentCache()]) is not None


def test_a_store_forgets_the_leftovers_of_a_write_that_never_finished(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    (store.directory / "abc.tmp.safetensors").write_text("partial", encoding="utf-8")
    reopened = budgeted_store(tmp_path, mlx_stub)
    assert list(reopened.directory.glob("*.tmp*")) == []


def test_a_store_stops_writing_once_its_session_budget_is_spent(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub, write_budget=1)
    store.remember(list(range(1000)), [TrimmableCache()])
    assert store.persist(list(range(1000))) == cache_state.COLD_WRITE_BUDGET_REACHED


def test_the_memory_tier_answers_before_the_disk_tier(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    ids = list(range(1000))
    store.remember(ids, [TrimmableCache()])
    result = store.restore_best(ids + [7, 7], lambda: [TrimmableCache()])
    assert result is not None and result[2] == "memory"
    assert result[1] == ids


def test_a_snapshot_that_is_not_a_prefix_is_not_offered(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    store.remember(list(range(1000)), [TrimmableCache()])
    assert store.restore_best([9] * 2000, lambda: [TrimmableCache()]) is None


def test_a_persisted_snapshot_survives_the_memory_tier_being_dropped(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    ids = list(range(1000))
    store.remember(ids, [TrimmableCache()])
    assert store.persist(ids) is None
    store.forget()
    result = store.restore_best(ids + [5], lambda: [TrimmableCache()])
    assert result is not None and result[2] == "disk"
    assert result[1] == ids


def test_the_longest_usable_snapshot_wins(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    short, long = list(range(400)), list(range(900))
    store.remember(short, [TrimmableCache()])
    store.persist(short)
    store.remember(long, [TrimmableCache()])
    store.persist(long)
    store.forget()
    result = store.restore_best(list(range(1200)), lambda: [TrimmableCache()])
    assert result is not None and len(result[1]) == 900


def test_a_snapshot_shorter_than_the_reuse_floor_is_ignored(tmp_path, mlx_stub):
    store = budgeted_store(tmp_path, mlx_stub)
    store.remember([1, 2, 3], [TrimmableCache()])
    assert store.restore_best([1, 2, 3, 4], lambda: [TrimmableCache()]) is None


# --------------------------------------------- interruption, end to end

def vlm_adapter_with(monkeypatch, mlx_module, cache, prior_ids, generated_tokens,
                     advance_to=None, cancel_after=None):
    """Drive MLXVLMAdapter.stream over a runtime that behaves like mlx-vlm.

    The stub honours the two interactions the real runtime has with the cache
    state -- it asks for a shared prefix, then advances the cache in place --
    because those are exactly what the settling logic reasons about.
    """
    from mlx_vlm_worker.adapter import MLXVLMAdapter

    mlx_vlm = ModuleType("mlx_vlm")
    prompt_utils = ModuleType("mlx_vlm.prompt_utils")
    generate = ModuleType("mlx_vlm.generate")
    generate.PromptCacheState = BasePromptCacheState
    prompt_utils.apply_chat_template = lambda *args, **kwargs: "rendered prompt"

    adapter = MLXVLMAdapter()

    def stream_generate(**kwargs):
        state = kwargs.get("prompt_cache_state")
        prompt_ids = list(prior_ids)
        if state is not None:
            state.find_prefix_length(prompt_ids)
        for index, token in enumerate(generated_tokens):
            # Only a cache that reports an offset gets one moved: assigning to a
            # component that has none would invent the very signal the settling
            # check looks for.
            if hasattr(cache[0], "offset"):
                cache[0].offset = (len(prompt_ids) + index + 1 if advance_to is None
                                   else advance_to)
            if cancel_after is not None and index == cancel_after:
                adapter.cancelled.add("request")
            yield SimpleNamespace(text=f"t{token}", token=token, prompt_tokens=len(prompt_ids),
                                  generation_tokens=index + 1, prompt_tps=100.0,
                                  generation_tps=10.0, cached_tokens=0, finish_reason=None)

    mlx_vlm.stream_generate = stream_generate
    mlx_vlm.load = lambda *args, **kwargs: (None, None)
    monkeypatch.setitem(sys.modules, "mlx_vlm", mlx_vlm)
    monkeypatch.setitem(sys.modules, "mlx_vlm.prompt_utils", prompt_utils)
    monkeypatch.setitem(sys.modules, "mlx_vlm.generate", generate)
    monkeypatch.setitem(sys.modules, "mlx", mlx_module)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_module.core)

    adapter.model = SimpleNamespace(config=object())
    adapter.processor = object()
    adapter.modalities = ["text"]
    adapter._reset_prompt_cache()
    adapter.prompt_cache_state.token_ids = list(prior_ids)
    adapter.prompt_cache_state.cache = cache
    return adapter


def test_an_interrupted_turn_keeps_its_cache_and_relabels_it(monkeypatch, mlx_stub):
    cache = [TrimmableCache(offset=3)]
    adapter = vlm_adapter_with(monkeypatch, mlx_stub, cache, [1, 2, 3], [7, 8, 9],
                               cancel_after=1)
    list(adapter.stream("request", {"prompt": "hello"}))
    # Two tokens were streamed before the cancel landed, and the cache is where
    # those two tokens say it is, so the pairing is provable and kept.
    assert adapter.prompt_cache_state.cache is cache
    assert adapter.prompt_cache_state.token_ids == [1, 2, 3, 7, 8]


def test_an_interrupted_turn_is_discarded_when_the_cache_is_not_where_it_should_be(
        monkeypatch, mlx_stub):
    cache = [TrimmableCache(offset=3)]
    adapter = vlm_adapter_with(monkeypatch, mlx_stub, cache, [1, 2, 3], [7, 8, 9],
                               advance_to=999, cancel_after=1)
    list(adapter.stream("request", {"prompt": "hello"}))
    assert adapter.prompt_cache_state.token_ids is None


def test_an_interruption_for_memory_pressure_releases_the_cache(monkeypatch, mlx_stub):
    cache = [TrimmableCache(offset=3)]
    adapter = vlm_adapter_with(monkeypatch, mlx_stub, cache, [1, 2, 3], [7, 8, 9],
                               cancel_after=1)
    adapter.note_abort("request", cache_state.COLD_MEMORY_PRESSURE)
    list(adapter.stream("request", {"prompt": "hello"}))
    assert adapter.prompt_cache_state.token_ids is None
    assert adapter._pending_cold_reason == cache_state.COLD_MEMORY_PRESSURE


def test_a_cache_without_an_offset_cannot_be_relabelled(monkeypatch, mlx_stub):
    cache = [RecurrentCache()]
    adapter = vlm_adapter_with(monkeypatch, mlx_stub, cache, [1, 2, 3], [7, 8, 9],
                               cancel_after=1)
    list(adapter.stream("request", {"prompt": "hello"}))
    assert adapter.prompt_cache_state.token_ids is None
    assert adapter._pending_cold_reason == cache_state.COLD_TOKEN_IDS_UNAVAILABLE


def test_the_reason_a_previous_turn_was_dropped_explains_the_next_cold_request(mlx_stub):
    from mlx_vlm_worker.adapter import MLXVLMAdapter

    adapter = MLXVLMAdapter()
    adapter._pending_cold_reason = cache_state.COLD_CANCELLED_PREVIOUS
    reason = adapter._cold_reason("cold", {"reason": cache_state.COLD_NO_PREFIX})
    assert reason == cache_state.COLD_CANCELLED_PREVIOUS
    # Consumed, so it explains one request rather than every later one.
    assert adapter._cold_reason("cold", {"reason": cache_state.COLD_NO_PREFIX}) == \
        cache_state.COLD_NO_PREFIX


def test_a_warm_request_reports_no_cold_reason(mlx_stub):
    from mlx_vlm_worker.adapter import MLXVLMAdapter

    adapter = MLXVLMAdapter()
    adapter._pending_cold_reason = cache_state.COLD_CANCELLED_PREVIOUS
    assert adapter._cold_reason("memory", {}) is None


def test_token_ids_ignore_values_that_are_not_ids():
    from mlx_vlm_worker.adapter import _collect_token

    collected: list[int] = []
    _collect_token(collected, SimpleNamespace(token=None))
    _collect_token(collected, SimpleNamespace(token=True))
    _collect_token(collected, SimpleNamespace())
    _collect_token(collected, SimpleNamespace(token=5))
    assert collected == [5]


def test_persisting_is_throttled_by_growth(mlx_stub):
    from mlx_vlm_worker.adapter import MLXVLMAdapter

    adapter = MLXVLMAdapter()
    assert adapter._should_persist(100) is False
    assert adapter._should_persist(4000) is True
    adapter._persisted_tokens = 4000
    assert adapter._should_persist(4200) is False
    assert adapter._should_persist(5100) is True


def test_the_namespace_sweep_leaves_the_checkpoint_store_alone(tmp_path, mlx_stub, monkeypatch):
    """The two stores share a parent directory, and only one owns the sweep.

    APC generations sit directly under the cache root; checkpoints keep theirs
    under a sibling `checkpoints/`. A sweep that treats every directory as a
    stale generation deletes the snapshots of the model that is loaded right
    now -- silently, at the moment a second generation appears.
    """
    from mlx_vlm_worker.adapter import MLXVLMAdapter, APC_NAMESPACE_PREFIX

    adapter = MLXVLMAdapter()
    adapter.apc_root = tmp_path
    adapter.apc_namespace = APC_NAMESPACE_PREFIX + "current"
    current = tmp_path / adapter.apc_namespace
    older = tmp_path / (APC_NAMESPACE_PREFIX + "older")
    oldest = tmp_path / (APC_NAMESPACE_PREFIX + "oldest")
    checkpoints = tmp_path / "checkpoints" / "mlxbar-vlm-ckpt-v1-current"
    for path in (current, older, oldest, checkpoints):
        path.mkdir(parents=True)
    (checkpoints / "snapshot.safetensors").write_text("kept", encoding="utf-8")
    import os, time
    now = time.time()
    os.utime(older, (now - 60, now - 60))
    os.utime(oldest, (now - 600, now - 600))

    removed = adapter._sweep_stale_namespaces(2)

    assert (checkpoints / "snapshot.safetensors").is_file()
    assert current.is_dir()
    assert removed == [oldest.name]
    assert adapter._namespace_count() == 2
