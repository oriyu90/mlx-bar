from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path

from .catalog.scanner import scan_all
from .database import Database
from .jobs import JobManager
from .runtimes.slots import SlotStore
from .runtimes.updater import RuntimeUpdater
from .runtimes.service import RuntimeUpdateService
from .settings import SettingsStore
from .workers.supervisor import WorkerSupervisor

CATALOG_CLASSIFIER_VERSION = "2"


class AppState:
    def __init__(self, root: Path | None = None):
        self.settings = SettingsStore(root)
        self.root = self.settings.root
        self.database = Database(self.root / "state.sqlite3")
        self.jobs = JobManager(self.database)
        self.workers = WorkerSupervisor(self.root, self.settings)
        self.slots = SlotStore(self.root)
        self.updater = RuntimeUpdater(self.slots)
        self.runtime_updates = RuntimeUpdateService(self.updater, self.slots, self.workers, self.database)
        self.runtime_update_jobs: dict[str, str] = {}
        self.model_autoload_lock = asyncio.Lock()
        self.listener = None
        self.public_listener_error: str | None = None
        self.started_at = asyncio.get_event_loop().time()

    def scan_job(self) -> dict:
        async def work(update):
            await update(0.1, "モデルフォルダを走査中")
            models = await scan_all(self.settings.data, self.settings.lm_studio_token)
            await update(0.9, f"{len(models)}件を保存中")
            await asyncio.to_thread(self.database.replace_models, models)
            await asyncio.to_thread(self.database.set_metadata_value,
                                    "catalog_classifier_version", CATALOG_CLASSIFIER_VERSION)
            return {"count": len(models)}
        return self.jobs.create("model_scan", work)

    @staticmethod
    def test_port(port: int, host: str = "127.0.0.1") -> dict:
        if not 1024 <= port <= 65535:
            return {"available": False, "code": "INVALID_PORT", "port": port}
        if host not in {"127.0.0.1", "0.0.0.0"}:
            return {"available": False, "code": "INVALID_HOST", "port": port}
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
            return {"available": True, "host": host, "port": port}
        except OSError as exc:
            return {"available": False, "code": "PORT_IN_USE", "port": port, "message": str(exc)}
        finally:
            sock.close()

    def next_port(self, start: int | None = None) -> int | None:
        for port in range(start or self.settings.data["api"]["port"] + 1, 65536):
            if self.test_port(port, self.settings.data["api"]["host"])["available"]:
                return port
        return None

    @staticmethod
    def lan_ipv4_addresses() -> list[str]:
        try:
            candidates = {entry[4][0] for entry in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
            )}
        except OSError:
            return []
        result = []
        for value in candidates:
            address = ipaddress.ip_address(value)
            if not (address.is_loopback or address.is_unspecified or address.is_link_local):
                result.append(value)
        return sorted(result)
