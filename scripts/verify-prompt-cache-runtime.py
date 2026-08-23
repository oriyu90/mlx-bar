"""Verify MLXBar's reuse policy against the installed mlx-vlm, without weights.

The unit tests replace `mlx.core`, so they can prove the shape of a decision but
not the semantics of an array -- and the fake runtime classes answer questions
the real ones raise on. Both v1.6.0 defects were of exactly that kind: an empty
`KVCache` cannot be asked for its `state`, and mlx-vlm reads `.cache` once
before it asks for a prefix length. This script asks the installed runtime
instead. It needs no weights, so run it on every release.

    VENV="$HOME/Library/Application Support/MLXBar/runtimes/mlx-vlm/slots/<slot>/.venv/bin/python"
    "$VENV" scripts/verify-prompt-cache-runtime.py     # from the repository root
"""
import sys
from pathlib import Path
sys.path.insert(0, "Workers")
import mlx.core as mx
from mlx_vlm.models import cache as rc
from mlx_vlm.generate import PromptCacheState
from mlx_vlm.generate.dispatch import _prefix_cache_trim_amount
from common import cache_state
from mlx_vlm_worker.prompt_cache import build_guarded_state

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok = ok and good
    print(f"  [{'ok ' if good else 'FAIL'}] {label}: {got!r}" + ("" if good else f"  (want {want!r})"))

def hybrid(tokens=0):
    """16 full-attention layers + 48 recurrent, the Qwen3.8 shape, in miniature."""
    caches = [rc.KVCache(), rc.ArraysCache(4)]
    if tokens:
        k = mx.zeros((1, 4, tokens, 64), dtype=mx.bfloat16)
        caches[0].update_and_fetch(k, k)
        caches[1][0] = mx.zeros((1, 8, 16), dtype=mx.bfloat16)
    return caches

print("1. capability probe on the cache the runtime builds at load")
empty = hybrid()
check("can_trim(empty hybrid)", cache_state.can_trim(empty), False)
check("can_capture(empty hybrid)", cache_state.can_capture(empty), True)
check("rollback_capability(empty hybrid)", cache_state.rollback_capability(empty),
      cache_state.ROLLBACK_CHECKPOINT)
check("rollback_capability(empty KVCache only)", cache_state.rollback_capability([rc.KVCache()]),
      cache_state.ROLLBACK_TRIM)

print("2. mlx-vlm never receives a length that makes it call trim()")
class Ctl:
    def __init__(self, restore=None): self.restore = restore
    def observe_prompt(self, ids): pass
    def restore_for(self, ids): return self.restore(ids) if self.restore else None

def replay(held_ids, cache_tokens, prompt, controller=None):
    """dispatch.py:844-859, verbatim in structure."""
    state = build_guarded_state(PromptCacheState, controller or Ctl())
    state.token_ids = list(held_ids)
    state.cache = hybrid(cache_tokens)
    state.begin_request()
    if state.cache is None:                                    # 844 (reads .cache)
        return "skipped", 0
    prefix_len = state.find_prefix_length(prompt)              # 845
    kv_cache = state.cache                                     # 846
    n_drop = _prefix_cache_trim_amount(kv_cache, prefix_len)   # 848
    would_trim = bool(0 < prefix_len < len(prompt) and n_drop is not None and n_drop)
    return ("TRIM" if would_trim else "safe"), prefix_len

for label, held, tokens, prompt in (
    ("continuation, cache in step", [1, 2, 3], 3, [1, 2, 3, 4]),
    ("continuation, cache ahead",   [1, 2, 3], 5, [1, 2, 3, 4]),
    ("branch",                      [1, 2, 3, 4], 4, [1, 2, 9]),
    ("branch, cache ahead",         [1, 2, 3, 4], 6, [1, 2, 9]),
    ("nothing shared",              [1, 2, 3], 3, [9, 9, 9]),
):
    verdict, prefix = replay(held, tokens, prompt)
    check(f"{label} (prefix={prefix})", verdict, "safe")

print("3. a restored snapshot reaches a caller that read .cache first")
snapshot = hybrid(2)
state = build_guarded_state(PromptCacheState, Ctl(restore=lambda ids: (snapshot, [1, 2], "memory")))
state.token_ids = [1, 2, 3, 4]
state.cache = hybrid(4)
state.begin_request()
early = state.cache                       # a caller that reads before asking
prefix = state.find_prefix_length([1, 2, 9, 9])
check("prefix length", prefix, 2)
check("the early reference now holds the snapshot", list(early) == list(snapshot), True)
check("and is still the object the state reports", early is state.cache, True)

print("4. capture and restore round-trip through real mx arrays")
live = hybrid(3)
payload = cache_state.capture(live)
restored = hybrid()
check("restore returns True", cache_state.restore(restored, payload), True)
check("offset survives", int(restored[0].offset), int(live[0].offset))
check("keys match", bool(mx.array_equal(restored[0].state[0], live[0].state[0])), True)
check("recurrent hole preserved", restored[1].state[1] is None, True)

print("5. a refused branch leaves a cache the runtime can still measure")
state = build_guarded_state(PromptCacheState, Ctl())
state.token_ids = [1, 2, 3, 4]
state.cache = hybrid(4)
state.begin_request()
check("prefix length", state.find_prefix_length([1, 2, 9]), 0)
check("cache is empty, not None", state.cache == [], True)
check("trim amount is computable", _prefix_cache_trim_amount(state.cache, 0), 0)
check("labels are dropped with it", state.token_ids, None)

print("6. a snapshot is written under the name the index records")
import tempfile, os
from mlx_vlm_worker.prompt_cache import CheckpointStore
root = Path(tempfile.mkdtemp())
store = CheckpointStore(root=root, namespace="ns",
                        budget=cache_state.model_cache_budget(
                            {"num_hidden_layers": 4, "num_key_value_heads": 4,
                             "head_dim": 64, "hidden_size": 256}),
                        max_bytes=8 << 30, keep_generations=2, write_budget_bytes=8 << 30)
ids = list(range(1024))
check("remember", store.remember(ids, hybrid(4)), True)
check("persist", store.persist(ids), None)
names = sorted(item.name for item in store.directory.iterdir())
check("no leftover temporaries", [n for n in names if ".tmp" in n], [])
check("snapshot is readable again",
      store.restore_best(ids + [7], lambda: hybrid()) is not None, True)

print("\nRESULT:", "all checks passed" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
