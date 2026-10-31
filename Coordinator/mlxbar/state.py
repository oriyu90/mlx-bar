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
from .omlx_setup import OMLXSetupService
from .runtimes.slots import SlotStore
from .runtimes.updater import RuntimeUpdater
from .runtimes.service import RuntimeUpdateService
from .rag import RagService
from .settings import SettingsStore
from .workers.model_pool import ModelPoolSupervisor

# v2.10.0: bumped to "3" so every existing install rescans its catalog at
# startup and picks up the corrected VLM classification (classifier.py).
# Bump this together with any change to `catalog/classifier.py` decision
# rules, or stale rows keep serving the wrong engine until a manual scan.
CATALOG_CLASSIFIER_VERSION = "3"
# v3.2.0 (ISS-2): the capability/catalog columns (`capabilities`,
# `runtimeFit`, `evidence`, `loaderId`) landed without changing the classifier
# version, whose value is pinned by an existing test (and by the v3.1.0
# upgrade check). A separate marker makes every existing install rescan once
# so those columns get filled in; bump it with any change to what
# `catalog/classifier.py` writes into them.
CATALOG_CAPABILITIES_VERSION = "1"


class AppState:
    def __init__(self, root: Path | None = None):
        self.settings = SettingsStore(root)
        self.root = self.settings.root
        self.database = Database(self.root / "state.sqlite3")
        self.jobs = JobManager(self.database)
        # Knowledge base (RAG). Dormant unless `rag.enabled`; opens its own
        # SQLite file lazily on the first collection.
        self.rag = RagService(self.root, self.settings)
        self.workers = ModelPoolSupervisor(self.root, self.settings, self.database)
        self.slots = SlotStore(self.root)
        self.updater = RuntimeUpdater(self.slots)
        self.runtime_updates = RuntimeUpdateService(self.updater, self.slots, self.workers, self.database)
        self.runtime_update_jobs: dict[str, str] = {}
        # oMLX server setup (v2.10.0): install/start/stop of the external
        # server app, exposed as cancellable jobs by the Runtime page's card.
        self.omlx_setup = OMLXSetupService()
        self.omlx_setup_jobs: dict[str, str] = {}
        self.model_autoload_lock = asyncio.Lock()
        self.listener = None
        self.management_server = None
        self.public_listener_error: str | None = None
        self.started_at = asyncio.get_event_loop().time()
        # Last context-compression event (see api/context_compression.py), or
        # None. In-memory only -- purely informational, not persisted, so a
        # restart simply forgets it rather than needing a DB migration.
        self.last_context_compression: dict | None = None
        # Last adaptive-memory planning event (see api/adaptive_memory/), or
        # None. Same in-memory, informational-only treatment.
        self.last_adaptive_memory: dict | None = None
        # Last request-time RAG retrieval (see api/openai_compat.py), or None.
        # In-memory only, purely informational -- same treatment as
        # last_context_compression.
        self.last_rag_retrieval: dict | None = None
        # v3.2.0 (ISS-3 / WP-311): outcome of the startup resident preload,
        # published by /api/v1/status as `preload` so the menu bar can say
        # "loading resident model..." / "could not load (reason)" instead of
        # the failure only existing in the log. In-memory only: a restart
        # re-runs the preload and overwrites it.
        self.preload_state: dict = {
            "state": "pending", "modelId": None, "reason": None,
            "code": None, "at": None,
        }

    def set_preload_state(self, state: str, *, model_id: str | None = None,
                          reason: str | None = None,
                          code: str | None = None) -> dict:
        """Record one preload transition (v3.2.0 / WP-311).

        `state` is one of pending / running / loaded / failed / skipped; the
        timestamp is a wall-clock epoch float so a client can age the row
        without trusting its own clock against ours.
        """
        if state not in {"pending", "running", "loaded", "failed", "skipped"}:
            state = "failed"
        self.preload_state = {
            "state": state,
            "modelId": model_id,
            "reason": reason,
            "code": code,
            "at": time.time(),
        }
        return dict(self.preload_state)

    def _roots_that_would_be_lost(self) -> list[str]:
        """Configured model roots that hold catalog rows but are unreachable.

        v3.3.3 (TASK-005): a rescan replaces the whole catalog, so a root that
        is temporarily unreadable -- an external volume that has not mounted
        yet at login, a sleeping disk -- contributes zero rows and every model
        under it disappears. The scan then stamps the version markers, so the
        loss is permanent: nothing rescans until the marker version changes
        again. A resident model in that catalog then fails the startup preload
        with `model_not_in_catalog`, silently, forever.

        Only roots that *currently* hold rows count: a root the user deleted
        from disk without removing it from settings keeps its old rows, but a
        root that never contributed anything is just an unhelpful setting and
        must not block scanning.
        """
        roots = [str(item) for item in self.settings.data["models"].get("roots", [])]
        if not roots:
            return []
        try:
            existing = self.database.list_models()
        except Exception:  # noqa: BLE001 - a catalog read failure is not fatal
            return []
        lost: list[str] = []
        for root in roots:
            try:
                path = Path(root).expanduser()
                if path.exists():
                    continue
                prefix = os.path.normcase(str(path))
            except (OSError, ValueError):
                continue
            if any(os.path.normcase(str(item.get("path") or "")).startswith(prefix)
                   for item in existing):
                lost.append(root)
        return lost

    def scan_job(self) -> dict:
        async def work(update):
            await update(0.1, "モデルフォルダを走査中")
            try:
                # Bounded for the same reason model reads are (v3.3.3 /
                # TASK-005): a TCC permission prompt for an external volume
                # blocks open()/stat() with no way to answer it from launchd.
                lost = await asyncio.wait_for(
                    asyncio.to_thread(self._roots_that_would_be_lost), timeout=15)
            except asyncio.TimeoutError:
                logging.getLogger(__name__).warning(
                    "Model scan skipped: the model roots did not answer within 15s "
                    "(an external-volume permission prompt may be waiting); keeping "
                    "the existing catalog")
                return {"count": 0, "skipped": "unresponsive_roots", "roots": []}
            if lost:
                # Keep the catalog (and its resident rows) intact and leave the
                # version markers alone, so the next start retries once the
                # volume is back. Destroying the catalog here is what turned a
                # "disk was not mounted yet" into a permanently broken resident
                # model.
                logging.getLogger(__name__).warning(
                    "Model scan skipped: %s unreachable but still holds catalog "
                    "entries; keeping the existing catalog until it is back",
                    ", ".join(lost))
                return {"count": 0, "skipped": "unavailable_roots", "roots": lost}
            models = await scan_all(self.settings.data, self.settings.lm_studio_token)
            await update(0.9, f"{len(models)}件を保存中")
            await asyncio.to_thread(self.database.replace_models, models)
            await asyncio.to_thread(self.database.set_metadata_value,
                                    "catalog_classifier_version", CATALOG_CLASSIFIER_VERSION)
            # v3.2.0 (ISS-2): separate marker for the capability columns. A
            # v3.1.0 install is already at classifier version "3", so without
            # its own marker it would never rescan and its catalog rows would
            # keep `capabilities` empty (the Ornith vision badge).
            await asyncio.to_thread(self.database.set_metadata_value,
                                    "catalog_capabilities_version",
                                    CATALOG_CAPABILITIES_VERSION)
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

    def omlx_setup_job(self, action: str) -> dict:
        """Install/start/stop the oMLX server as a cancellable job.

        Same 202-plus-`/jobs/{id}` plumbing the runtime page already uses, so
        progress, SSE events and cancel reuse existing machinery. One job per
        action: a second submit while the same action is queued/running
        returns the existing job instead of stacking a second brew install.
        Different actions serialize inside the service's own lock.
        """
        if action not in {"install", "start", "stop"}:
            raise ValueError("unsupported oMLX setup action")
        existing_id = self.omlx_setup_jobs.get(action)
        if existing_id:
            existing = self.database.get_job(existing_id)
            if existing and existing["state"] in {"queued", "running"}:
                return existing

        async def work(update):
            if action == "install":
                return await self.omlx_setup.install(update)
            if action == "start":
                return await self.omlx_setup.start_server(update, verify=self.omlx_connect_probe)
            return await self.omlx_setup.stop_server(update, verify=self.omlx_connect_probe)

        job = self.jobs.create(f"omlx_{action}", work)
        self.omlx_setup_jobs[action] = job["id"]
        return job

    async def omlx_connect_probe(self):
        """Reachability truth source for `omlx start/stop` jobs.

        Returns the provider's own OMLXStatus (or None when this pool has no
        provider to probe); its `connected` field decides whether the action
        is confirmed. The probe never raises — the adapter folds connection
        failures into the status object — so the start poll stays a wait, not
        an error, no matter what the configured server URL is doing.
        """
        provider = getattr(self.workers, "_omlx_provider", None)
        if provider is None:
            return None
        try:
            return await provider.connect()
        except Exception:  # noqa: BLE001 - unreachability is not job failure
            return None

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
        with contextlib.suppress(Exception):
            self.rag.close()
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
