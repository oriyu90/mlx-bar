from __future__ import annotations

import json
import os
import sqlite3
import threading
import unicodedata
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, path TEXT,
  provider_key TEXT, format TEXT NOT NULL, engine TEXT, modalities TEXT NOT NULL,
  confidence REAL NOT NULL, reason TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, progress REAL,
  message TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS runtime_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, engine TEXT NOT NULL, slot_id TEXT NOT NULL,
  action TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS api_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT, method TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0, model TEXT, stream INTEGER NOT NULL DEFAULT 0,
  message_count INTEGER NOT NULL DEFAULT 0, tool_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT, client_scope TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def replace_models(self, models: list[dict]) -> None:
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM models")
            self.connection.executemany(
                """INSERT INTO models
                (id,source,name,path,provider_key,format,engine,modalities,confidence,reason,size_bytes)
                VALUES (:id,:source,:name,:path,:provider_key,:format,:engine,:modalities,:confidence,:reason,:size_bytes)""",
                [{**m, "modalities": json.dumps(m["modalities"])} for m in models],
            )

    def metadata_value(self, key: str) -> str | None:
        with self.lock:
            row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata_value(self, key: str, value: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
            )

    def list_models(self) -> list[dict]:
        with self.lock:
            rows = self.connection.execute("SELECT * FROM models ORDER BY name COLLATE NOCASE").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["modalities"] = json.loads(item["modalities"])
            result.append(item)
        return result

    def get_model(self, model_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["modalities"] = json.loads(item["modalities"])
        return item

    def has_duplicate_model_paths(self) -> bool:
        with self.lock:
            rows = self.connection.execute("SELECT path FROM models WHERE path IS NOT NULL AND path != ''").fetchall()
        seen: set[str] = set()
        for row in rows:
            key = os.path.normcase(str(Path(row["path"]).expanduser().resolve()))
            if key in seen:
                return True
            seen.add(key)
        return False

    def has_mergeable_pathless_providers(self) -> bool:
        """Return true when a legacy provider row can be folded into one local row.

        Older catalog builds kept LM Studio API entries without a path separate
        from the same model discovered on disk.  Triggering a startup scan here
        repairs those existing databases after an app update.
        """
        with self.lock:
            rows = self.connection.execute(
                "SELECT source, name, path FROM models"
            ).fetchall()

        def normalized_name(value: str) -> str:
            return unicodedata.normalize("NFKC", value).strip().casefold()

        local_counts: dict[str, int] = {}
        providers: list[str] = []
        for row in rows:
            name = normalized_name(row["name"])
            if row["path"]:
                local_counts[name] = local_counts.get(name, 0) + 1
            elif row["source"] == "lm_studio_api":
                providers.append(name)
        return any(local_counts.get(name) == 1 for name in providers)

    def upsert_job(self, job: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO jobs(id,kind,state,progress,message,result,error)
                VALUES(:id,:kind,:state,:progress,:message,:result,:error)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state,progress=excluded.progress,
                message=excluded.message,result=excluded.result,error=excluded.error,updated_at=CURRENT_TIMESTAMP""",
                {**job, "result": json.dumps(job.get("result")) if job.get("result") is not None else None,
                 "error": json.dumps(job.get("error")) if job.get("error") is not None else None},
            )

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("result", "error"):
            item[key] = json.loads(item[key]) if item[key] else None
        return item

    def list_active_runtime_jobs(self) -> dict[str, dict]:
        with self.lock:
            rows = self.connection.execute(
                """SELECT * FROM jobs
                   WHERE (kind LIKE 'runtime_update:%' OR kind LIKE 'runtime_stage:%')
                     AND state IN ('queued','running')
                   ORDER BY created_at DESC"""
            ).fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            item = dict(row)
            engine = item["kind"].split(":", 1)[1]
            if engine in result:
                continue
            for key in ("result", "error"):
                item[key] = json.loads(item[key]) if item[key] else None
            result[engine] = item
        return result

    def fail_incomplete_jobs(self) -> None:
        with self.lock, self.connection:
            rows = self.connection.execute(
                "SELECT * FROM jobs WHERE state IN ('queued','running')"
            ).fetchall()
            for row in rows:
                item = dict(row)
                item.update(state="failed", message="サービス再起動により中断されました",
                            error=json.dumps({"code": "JOB_INTERRUPTED",
                                              "message": "サービス再起動により処理が中断されました"},
                                             ensure_ascii=False))
                self.connection.execute(
                    """UPDATE jobs SET state=:state,message=:message,error=:error,
                       updated_at=CURRENT_TIMESTAMP WHERE id=:id""", item
                )

    def add_runtime_history(self, engine: str, slot_id: str, action: str, result: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO runtime_history(engine,slot_id,action,result) VALUES(?,?,?,?)",
                (engine, slot_id, action, json.dumps(result, ensure_ascii=False)),
            )

    def list_runtime_history(self, engine: str | None = None, limit: int = 50) -> list[dict]:
        with self.lock:
            if engine:
                rows = self.connection.execute(
                    "SELECT * FROM runtime_history WHERE engine=? ORDER BY id DESC LIMIT ?", (engine, limit)
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM runtime_history ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result

    def add_api_log(self, entry: dict, maximum: int = 2000) -> None:
        safe = {
            "request_id": str(entry.get("request_id") or "")[:96],
            "method": str(entry.get("method") or "")[:12],
            "path": str(entry.get("path") or "")[:256],
            "status": int(entry.get("status") or 0),
            "duration_ms": max(0, int(entry.get("duration_ms") or 0)),
            "model": str(entry.get("model") or "")[:256] or None,
            "stream": 1 if entry.get("stream") else 0,
            "message_count": max(0, int(entry.get("message_count") or 0)),
            "tool_count": max(0, int(entry.get("tool_count") or 0)),
            "error_code": str(entry.get("error_code") or "")[:96] or None,
            "client_scope": "lan" if entry.get("client_scope") == "lan" else "local",
        }
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO api_logs(request_id,method,path,status,duration_ms,model,stream,
                   message_count,tool_count,error_code,client_scope)
                   VALUES(:request_id,:method,:path,:status,:duration_ms,:model,:stream,
                   :message_count,:tool_count,:error_code,:client_scope)""", safe
            )
            self.connection.execute(
                "DELETE FROM api_logs WHERE id NOT IN (SELECT id FROM api_logs ORDER BY id DESC LIMIT ?)",
                (max(100, min(int(maximum), 10000)),),
            )

    def list_api_logs(self, limit: int = 500) -> list[dict]:
        limit = max(1, min(int(limit), 10000))
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM api_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_api_logs(self) -> int:
        with self.lock, self.connection:
            count = self.connection.execute("SELECT COUNT(*) FROM api_logs").fetchone()[0]
            self.connection.execute("DELETE FROM api_logs")
        return int(count)
