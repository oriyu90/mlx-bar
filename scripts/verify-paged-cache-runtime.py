"""Weight-free, real-MLX verification for the opt-in mlx-lm paged KV store.

Run this with every active/staged mlx-lm runtime slot. It uses tiny arrays and
does not load model weights.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "Workers")

import mlx.core as mx
from mlx_lm.models.cache import KVCache, QuantizedKVCache, RotatingKVCache

from mlx_lm_worker.paged_cache.capabilities import probe_plain_kv
from mlx_lm_worker.paged_cache.store import PagedKVStore


ok = True


def check(label, condition):
    global ok
    good = bool(condition)
    ok = ok and good
    print(f"  [{'ok ' if good else 'FAIL'}] {label}")


class TinyModel:
    layers = [object(), object()]


print("1. fail-closed capability registry")
check("plain KVCache is eligible", probe_plain_kv([KVCache(), KVCache()]).eligible)
check("quantized KVCache is not eligible",
      not probe_plain_kv([QuantizedKVCache(), QuantizedKVCache()]).eligible)
check("rotating KVCache is not eligible",
      not probe_plain_kv([RotatingKVCache(max_size=512), RotatingKVCache(max_size=512)]).eligible)

print("2. real MLX slice, safetensors, load, concatenate and restore")
model = TinyModel()
cache = [KVCache(), KVCache()]
tokens = list(range(600))
for index, layer in enumerate(cache):
    keys = mx.arange(2 * 600 * 4, dtype=mx.float16).reshape(1, 2, 600, 4) + index
    values = keys + 10
    layer.update_and_fetch(keys, values)
root = Path(tempfile.mkdtemp(prefix="mlxbar-paged-runtime-"))
os.environ["MLXBAR_MLX_MEMORY_LIMIT_BYTES"] = str(1 << 30)
store = PagedKVStore(
    model=model, model_fingerprint="weight-free-test", runtime_version="installed",
    root=str(root), max_bytes=1 << 28, disk_enabled=True, branch_reuse=True,
    cache_budget={"known": True, "perTokenBytes": 64, "fixedBytes": 0})
store.store(tokens, cache, prompt_length=len(tokens))
check("two full blocks were persisted", store.stats()["diskBlocks"] == 2)
restored = store.fetch(model, tokens)
check("512 tokens were restored", restored is not None and restored[1] == 512)
if restored:
    restored_cache = restored[0]
    check("every layer reports the restored offset",
          all(int(layer.offset) == 512 for layer in restored_cache))
    expected = cache[0].keys[..., :512, :]
    check("restored values equal the original block data",
          bool(mx.array_equal(restored_cache[0].keys, expected)))

print("3. branch prefix and namespace isolation")
branch = tokens[:256] + [42_000] * (len(tokens) - 256)
branch_hit = store.fetch(model, branch)
check("a changed second block restores only the first block",
      branch_hit is not None and branch_hit[1] == 256)
other = PagedKVStore(
    model=model, model_fingerprint="different-model", runtime_version="installed",
    root=str(root), max_bytes=1 << 28, disk_enabled=True, branch_reuse=True,
    cache_budget={"known": True, "perTokenBytes": 64, "fixedBytes": 0})
check("a different model namespace cannot see the blocks", other.fetch(model, tokens) is None)
check("no temporary safetensors remain", not list(root.rglob("*.tmp.safetensors")))

print("4. crash recovery and allocation admission")
first_block = sorted(store.disk.directory.glob("*.safetensors"))[0]
store.disk._checksum_path(first_block).unlink()
store.store(tokens, cache, prompt_length=len(tokens))
check("a half-renamed block heals without a worker restart",
      store.disk._checksum_path(first_block).is_file())
guarded = PagedKVStore(
    model=model, model_fingerprint="memory-guard-test", runtime_version="installed",
    root=str(root), max_bytes=1 << 28, disk_enabled=True, branch_reuse=True,
    cache_budget={"known": True, "perTokenBytes": 64, "fixedBytes": 0})
os.environ["MLXBAR_MLX_MEMORY_LIMIT_BYTES"] = "1"
guarded.store(tokens, cache, prompt_length=len(tokens))
check("a block allocation is skipped when headroom is insufficient",
      guarded.stats()["diskBlocks"] == 0 and guarded.stats()["storeBudgetRejects"] == 1)

print("\nRESULT:", "all checks passed" if ok else "FAILURES ABOVE")
shutil.rmtree(root, ignore_errors=True)
sys.exit(0 if ok else 1)
