from __future__ import annotations

import json
import builtins
import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from mlx_lm_worker.paged_cache.hashing import ROOT_PARENT, chain, chained_hash  # noqa: E402


class FakeArray:
    def __init__(self, data, dtype="float16", marker=0):
        if isinstance(data, FakeArray):
            dtype, marker, data = data.dtype, data.marker, data.shape
        self._shape = tuple(int(item) for item in data)
        self.dtype = str(dtype)
        self.marker = marker

    @property
    def shape(self):
        return self._shape

    @property
    def nbytes(self):
        size = 1
        for value in self.shape:
            size *= value
        return size * 2

    def __getitem__(self, item):
        if (not isinstance(item, tuple) or len(item) != 3 or item[0] is not Ellipsis
                or not isinstance(item[1], slice)):
            raise TypeError("fake supports only sequence-axis slicing")
        start = item[1].start or 0
        stop = item[1].stop if item[1].stop is not None else self.shape[2]
        shape = (self.shape[0], self.shape[1], max(0, stop - start), self.shape[3])
        return FakeArray(shape, self.dtype, self.marker + start)


class KVCache:
    def __init__(self):
        self.keys = self.values = None
        self.offset = 0

    @property
    def state(self):
        return self.keys, self.values, self.offset

    @state.setter
    def state(self, value):
        self.keys, self.values, self.offset = value


@pytest.fixture()
def fake_runtime(monkeypatch):
    mlx = ModuleType("mlx")
    mx = ModuleType("mlx.core")
    mx.array = FakeArray
    mx.contiguous = lambda value: FakeArray(value)
    mx.eval = lambda *values: None
    mx.zeros = lambda shape: FakeArray(shape)
    mx.get_active_memory = lambda: 0
    def concatenate(values, axis):
        assert axis == 2
        shape = list(values[0].shape)
        shape[2] = sum(value.shape[2] for value in values)
        return FakeArray(shape, values[0].dtype, values[0].marker)
    mx.concatenate = concatenate

    def save(path, arrays, metadata=None):
        payload = {"arrays": {name: {"shape": value.shape, "dtype": value.dtype,
                                              "marker": value.marker}
                              for name, value in arrays.items()}, "metadata": metadata or {}}
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    def load(path, return_metadata=False):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        arrays = {name: FakeArray(value["shape"], value["dtype"], value["marker"])
                  for name, value in payload["arrays"].items()}
        return (arrays, payload["metadata"]) if return_metadata else arrays

    mx.save_safetensors = save
    mx.load = load
    mlx.core = mx
    cache_module = ModuleType("mlx_lm.models.cache")
    cache_module.KVCache = KVCache
    cache_module.make_prompt_cache = lambda model: [KVCache() for _ in model.layers]
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(sys.modules, "mlx_lm", ModuleType("mlx_lm"))
    monkeypatch.setitem(sys.modules, "mlx_lm.models", ModuleType("mlx_lm.models"))
    monkeypatch.setitem(sys.modules, "mlx_lm.models.cache", cache_module)
    monkeypatch.setenv("MLXBAR_MLX_MEMORY_LIMIT_BYTES", str(1 << 30))
    return SimpleNamespace(mx=mx, model=SimpleNamespace(layers=[object(), object()]))


def populated_cache(layers=2, offset=600, capacity=768):
    result = []
    for layer_index in range(layers):
        cache = KVCache()
        cache.state = (FakeArray((1, 2, capacity, 4), marker=layer_index),
                       FakeArray((1, 2, capacity, 4), marker=100 + layer_index), offset)
        result.append(cache)
    return result


def make_store(tmp_path, fake_runtime, **overrides):
    from mlx_lm_worker.paged_cache.store import PagedKVStore
    options = dict(model=fake_runtime.model, model_fingerprint="model", runtime_version="1",
                   root=str(tmp_path), max_bytes=1 << 30, disk_enabled=True,
                   branch_reuse=True,
                   cache_budget={"known": True, "perTokenBytes": 64, "fixedBytes": 0})
    options.update(overrides)
    return PagedKVStore(**options)


def test_hash_chain_is_parent_and_order_sensitive():
    first = chained_hash(ROOT_PARENT, [1, 2, 3], "ns")
    assert first == chained_hash(ROOT_PARENT, [1, 2, 3], "ns")
    assert first != chained_hash(ROOT_PARENT, [1, 3, 2], "ns")
    assert first != chained_hash("1" * 64, [1, 2, 3], "ns")


@pytest.mark.parametrize("length,blocks", [(0, 0), (1, 0), (255, 0), (256, 1), (257, 1)])
def test_block_boundaries(length, blocks):
    assert len(chain(list(range(length)), "ns")) == blocks


def test_plain_blocks_round_trip_and_leave_a_tail(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    store.store(tokens, populated_cache(), prompt_length=600)
    assert store.stats()["diskBlocks"] == 2
    restored, length = store.fetch(fake_runtime.model, tokens)
    assert length == 512
    assert all(layer.offset == 512 for layer in restored)
    assert restored[0].keys.shape == (1, 2, 512, 4)
    assert tokens[length:] == list(range(512, 600))


def test_branch_uses_only_the_contiguous_common_blocks(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    original = list(range(600))
    store.store(original, populated_cache(), prompt_length=600)
    branch = original[:256] + [999] * 344
    _, length = store.fetch(fake_runtime.model, branch)
    assert length == 256


def test_disabling_branch_reuse_allows_only_an_exact_prompt(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime, branch_reuse=False)
    original = list(range(600))
    store.store(original, populated_cache(), prompt_length=600)
    assert store.fetch(fake_runtime.model, original)[1] == 512
    branch = original[:512] + [999] * 88
    assert store.fetch(fake_runtime.model, branch) is None


def test_corrupt_later_block_is_quarantined_without_losing_prior_prefix(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    store.store(tokens, populated_cache(), prompt_length=600)
    descriptors = chain(tokens[:512], store.semantic_salt)
    store.disk.path(descriptors[1]["hash"]).write_text("broken", encoding="utf-8")
    _, length = store.fetch(fake_runtime.model, tokens)
    assert length == 256
    assert store.stats()["corruptBlocks"] == 1
    assert not store.disk.path(descriptors[1]["hash"]).exists()


def test_unknown_memory_budget_never_creates_a_disk_store(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime, cache_budget={"known": False})
    assert store.disk is None
    assert store.stats()["enabled"] is False


def test_non_plain_layout_is_fail_closed(tmp_path, fake_runtime, monkeypatch):
    cache_module = sys.modules["mlx_lm.models.cache"]
    cache_module.make_prompt_cache = lambda model: [SimpleNamespace()]
    store = make_store(tmp_path, fake_runtime)
    assert store.capability.eligible is False
    assert store.disk is None


def test_disabled_facade_does_not_import_or_create_the_paged_backend(
        tmp_path, fake_runtime, monkeypatch):
    from mlx_lm_worker.prompt_cache import PromptCacheStore
    imported = []
    original_import = builtins.__import__

    def watched_import(name, *args, **kwargs):
        if "paged_cache" in name:
            imported.append(name)
        return original_import(name, *args, **kwargs)

    paged_root = tmp_path / "must-not-exist"
    monkeypatch.setattr(builtins, "__import__", watched_import)
    store = PromptCacheStore(
        str(tmp_path), "runtime", disk_enabled=False, model=fake_runtime.model,
        paged_enabled=False, paged_root=str(paged_root),
        cache_budget={"known": True, "perTokenBytes": 64})
    assert store.paged is None
    assert imported == []
    assert not paged_root.exists()


def test_pruning_counts_checksum_sidecars_and_converges_to_quota(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    store.store(tokens, populated_cache(), prompt_length=600)
    files = list(store.disk.directory.glob("*.safetensors"))
    one_block_budget = max(
        path.stat().st_size + store.disk._checksum_path(path).stat().st_size for path in files)
    store.disk.max_bytes = one_block_budget
    store.disk.prune()
    blocks, disk_bytes = store.disk.stats()
    assert blocks == 1
    assert disk_bytes <= one_block_budget


def test_store_rejects_temporary_block_when_memory_headroom_is_insufficient(
        tmp_path, fake_runtime, monkeypatch):
    store = make_store(tmp_path, fake_runtime)
    monkeypatch.setenv("MLXBAR_MLX_MEMORY_LIMIT_BYTES", "1")
    store.store(list(range(600)), populated_cache(), prompt_length=600)
    assert store.stats()["diskBlocks"] == 0
    assert store.stats()["storeBudgetRejects"] == 1
    assert store.stats()["lastMissReason"] == "store_budget_rejected"


def test_unknown_active_memory_is_fail_closed_for_store_and_restore(
        tmp_path, fake_runtime, monkeypatch):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    store.store(tokens, populated_cache(), prompt_length=600)
    assert store.stats()["diskBlocks"] == 2
    monkeypatch.delattr(fake_runtime.mx, "get_active_memory")
    assert store.fetch(fake_runtime.model, tokens) is None
    assert store.stats()["restoreBudgetRejects"] == 1

    empty = make_store(tmp_path / "empty", fake_runtime)
    empty.store(tokens, populated_cache(), prompt_length=600)
    assert empty.stats()["diskBlocks"] == 0
    assert empty.stats()["storeBudgetRejects"] == 1


def test_half_renamed_block_is_repaired_without_restart(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    descriptor = chain(tokens[:512], store.semantic_salt)[0]
    orphan = store.disk.path(descriptor["hash"])
    orphan.write_text("orphan", encoding="utf-8")
    assert not store.disk.exists(descriptor["hash"])
    store.store(tokens, populated_cache(), prompt_length=600)
    assert store.disk.exists(descriptor["hash"])
    assert orphan.read_text(encoding="utf-8") != "orphan"


def test_startup_removes_orphan_checksum(tmp_path, fake_runtime):
    store = make_store(tmp_path, fake_runtime)
    orphan = store.disk.directory / (("a" * 64) + ".safetensors.sha256")
    orphan.write_text("0" * 64, encoding="ascii")
    assert orphan.exists()
    make_store(tmp_path, fake_runtime)
    assert not orphan.exists()


def test_oversized_corrupt_block_is_quarantined_before_runtime_load(
        tmp_path, fake_runtime, monkeypatch):
    store = make_store(tmp_path, fake_runtime)
    tokens = list(range(600))
    store.store(tokens, populated_cache(), prompt_length=600)
    descriptor = chain(tokens[:512], store.semantic_salt)[0]
    path = store.disk.path(descriptor["hash"])
    path.write_bytes(b"x" * ((1 << 20) + 65537))
    store.disk._checksum_path(path).write_text(
        hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="ascii")
    loaded = []
    monkeypatch.setattr(fake_runtime.mx, "load", lambda *args, **kwargs: loaded.append(args))
    assert store.fetch(fake_runtime.model, tokens) is None
    assert loaded == []
    assert store.stats()["corruptBlocks"] == 1
    assert not path.exists()


def test_symlink_cache_root_is_fail_closed(tmp_path, fake_runtime):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "link"
    root.symlink_to(target, target_is_directory=True)
    store = make_store(root, fake_runtime)
    assert store.disk is None
    assert store.stats()["enabled"] is False
    assert store.stats()["disabledReason"].startswith("disk_init_failed:")
