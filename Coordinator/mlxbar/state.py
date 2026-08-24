from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import signal
import socket
import time
from pathlib import Path

from .catalog.scanner import scan_all
from .database import Database
from .jobs import JobManager
from .runtimes.slots import SlotStore
from .runtimes.updater import RuntimeUpdater
from .runtimes.service import RuntimeUpdateService
from .settings import SettingsStore
from .workers.model_pool import ModelPoolSupervisor

CATALOG_CLASSIFIER_VERSION = "2"


class AppState:
    def __init__(self, root: Path | None = None):
        self.settings = SettingsStore(root)
        self.root = self.settings.root
        self.database = Database(self.root / "state.sqlite3")
        self.jobs = JobManager(self.database)
        self.workers = ModelPoolSupervisor(self.root, self.settings)
        self.slots = SlotStore(self.root)
        self.updater = RuntimeUpdater(self.slots)
        self.runtime_updates = RuntimeUpdateService(self.updater, self.slots, self.workers, self.database)
        self.runtime_update_jobs: dict[str, str] = {}
        self.model_autoload_lock = asyncio.Lock()
        self.listener = None
        self.management_server = None
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

    def runtime_update_job(self, engine: str) -> dict:
        if engine not in {"mlx-lm", "mlx-vlm"}:
            raise ValueError("unsupported runtime")
        existing_id = self.runtime_update_jobs.get(engine)
        if existing_id:
            existing = self.database.get_job(existing_id)
            if existing and existing["state"] in {"queued", "running"}:
                return existing

        async def work(update):
            result = await self.runtime_updates.update_latest(engine, update)
            check = result.get("check")
            if check:
                check["checkedAt"] = time.time()
                self.database.set_metadata_value(f"runtime_check:{engine}", json.dumps(check))
            return result

        job = self.jobs.create(f"runtime_update:{engine}", work)
        self.runtime_update_jobs[engine] = job["id"]
        return job

    def install_missing_runtimes(self) -> list[dict]:
        if not self.settings.data.get("runtimes", {}).get("autoInstallMissing", True):
            return []
        return [self.runtime_update_job(engine) for engine in ("mlx-lm", "mlx-vlm")
                if not self.slots.active(engine).get("active")]

    async def reset_all(self) -> None:
        """Cancels all work, wipes every file this coordinator owns, and
        triggers its own graceful shutdown.

        Reuses the exact same signal-driven teardown `serve()`'s `finally`
        block already runs on SIGTERM (cancel_all/unload/shutdown are
        idempotent, so running them here first and again there is harmless).
        The socket this request arrived on is deliberately left alone here --
        it's still bound and in use to send the response -- and is unlinked
        last by that existing shutdown sequence, exactly as it already does
        today via `socket_path.unlink(missing_ok=True)`.
        """
        with contextlib.suppress(Exception):
            await self.jobs.cancel_all()
        with contextlib.suppress(Exception):
            await self.workers.unload()
        with contextlib.suppress(Exception):
            await self.workers.shutdown()
        shutil.rmtree(self.workers.socket_dir, ignore_errors=True)
        with contextlib.suppress(Exception):
            self.database.close()
        for handler in list(logging.getLogger().handlers):
            with contextlib.suppress(Exception):
                handler.close()
            logging.getLogger().removeHandler(handler)
        control = self.root / "control"
        for entry in self.root.iterdir():
            if entry.name == "control":
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        if control.exists():
            for entry in control.iterdir():
                if entry.name == "coordinator.sock":
                    continue
                with contextlib.suppress(OSError):
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        if self.management_server is not None:
            # Directly triggers the same graceful-shutdown flag `handle_exit`
            # (the SIGTERM/SIGINT handler) sets, without going through actual
            # signal delivery -- self-signalling from inside the very request
            # handler asyncio is running turned out not to reliably wake the
            # event loop's signal-handling path in testing.
            self.management_server.should_exit = True
        else:
            os.kill(os.getpid(), signal.SIGTERM)

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
