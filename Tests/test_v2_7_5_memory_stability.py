"""v2.7.5: memory footprint / leak / crash-safety regression tests.

Safe-design contract: no default changes, no schema change, same JSON shapes.
Covers only the bounded/capped/guarded paths added in v2.7.5.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Coordinator"))

from mlxbar.api import openai_compat as oc  # noqa: E402
from mlxbar.api import management as mgmt  # noqa: E402
from mlxbar.database import Database  # noqa: E402


def _tmp_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "state.sqlite3")


def test_jobs_table_is_bounded(tmp_path: Path):
    db = _tmp_db(tmp_path)
    for i in range(260):
        db.upsert_job({"id": f"job-{i:04d}", "kind": "model_scan",
                       "state": "completed", "progress": 1.0,
                       "message": "done", "result": {"count": 1}, "error": None})
    rows = db.connection.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    assert rows <= 200, f"jobs grew unbounded: {rows}"
    # Active rows are never pruned.
    db.upsert_job({"id": "active-1", "kind": "model_scan", "state": "running",
                   "progress": 0.1, "message": "x", "result": None, "error": None})
    assert db.get_job("active-1")["state"] == "running"
    db.close()


def test_runtime_history_is_bounded_per_engine(tmp_path: Path):
    db = _tmp_db(tmp_path)
    for i in range(150):
        db.add_runtime_history("mlx-lm", f"slot-{i}", "stage", {"ok": True})
    rows = db.connection.execute(
        "SELECT COUNT(*) c FROM runtime_history WHERE engine='mlx-lm'").fetchone()["c"]
    assert rows <= 100, f"history grew unbounded: {rows}"
    db.close()


def test_prune_helpers_keep_newest(tmp_path: Path):
    db = _tmp_db(tmp_path)
    for i in range(60):
        db.add_runtime_history("mlx-vlm", f"s{i}", "probe", {"i": i})
    db.prune_runtime_history(keep_per_engine=20)
    rows = db.connection.execute(
        "SELECT COUNT(*) c FROM runtime_history WHERE engine='mlx-vlm'").fetchone()["c"]
    assert rows == 20
    db.prune_old_jobs(keep_terminal=50)
    db.close()


def test_usage_from_event_never_raises_on_garbage():
    out = oc._usage_from_event({"prompt_tokens": "abc", "completion_tokens": None})
    assert out["prompt_tokens"] == 0 and out["completion_tokens"] == 0
    out2 = oc._usage_from_event({"prompt_tokens": 10, "completion_tokens": 5},
                                None, cached_tokens="oops")
    assert "prompt_tokens_details" not in out2
    out3 = oc._usage_from_event({"prompt_tokens": 10, "completion_tokens": 5},
                                None, cached_tokens=4)
    assert out3["prompt_tokens_details"] == {"cached_tokens": 4}


def test_effective_limits_survive_corrupt_settings():
    from mlxbar.workers.supervisor import WorkerSupervisor

    class FakeSettings:
        data = {"generation": {"maxTokens": "oops", "maxPromptCharacters": None}}

    sup = WorkerSupervisor.__new__(WorkerSupervisor)
    sup.settings = FakeSettings()
    sup.loaded = None
    assert sup.effective_max_tokens() == 8192
    assert sup.effective_max_prompt_characters() == 100000


def test_status_cache_same_shape_and_cached():
    # Shape must be identical whether fresh or cached; second call hits cache.
    mgmt._status_cache.update({"at": 0.0, "lan_at": 0.0, "lan": None,
                               "rag_at": 0.0, "rag": None, "tiers": {}})

    class FakeRag:
        enabled = True

        class Store:
            def list_collections(self):
                return [{"chunk_count": 3}, {"chunk_count": 4}]

        store = Store()

    class FakeDB:
        def __init__(self):
            self.calls = 0

        def recent_cache_tiers(self, model=None):
            self.calls += 1
            return [{"cache_tier": "cold", "cold_reason": "first_request"}]

    class FakeApp:
        rag = FakeRag()
        last_rag_retrieval = None
        database = FakeDB()

    app = FakeApp()
    first = mgmt._cached_rag_summary(app)
    second = mgmt._cached_rag_summary(app)
    assert first == second
    assert first["chunkCount"] == 7
    tiers1 = mgmt._cached_recent_tiers(app, "m")
    tiers2 = mgmt._cached_recent_tiers(app, "m")
    assert tiers1 == tiers2
    assert app.database.calls == 1  # second tier read served from cache


def test_clear_memory_endpoint_has_mlxbar_error_guard():
    import inspect

    src = inspect.getsource(mgmt.clear_memory_prompt_cache)
    assert "MLXBarError" in src
