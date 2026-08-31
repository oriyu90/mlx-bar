from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import MLXBarError
from .supervisor import QueuedRequest, WorkerSupervisor as SingleWorkerSupervisor


GIB = 1 << 30
LOGGER = logging.getLogger(__name__)


@dataclass
class PoolSlot:
    model: dict
    worker: SingleWorkerSupervisor
    reservation_bytes: int
    # v1.8.0 same-model replicas: several PoolSlots may share one model id, each
    # its own worker process (one MLX process is single-threaded, so this is the
    # only way distinct generations of the *same* model run at once). Slot 0 is
    # byte-for-byte the pre-v1.8.0 slot; replicas 1..N carry a suffixed
    # instance_key so their manifest, log and socket never collide.
    replica_index: int = 0
    keep_loaded: bool = False
    session_pinned: bool = False
    leases: int = 0
    last_released_at: float = 0.0
    state: str = "loading"
    load_future: asyncio.Future | None = None
    # v1.7.0 cross-model generation: this lane serialises requests to *this*
    # model (one MLX process is single-threaded) while a pool-wide semaphore
    # bounds how many lanes run at once.  gen_permit records whether this lane
    # currently holds one of those semaphore permits, so orphan recovery never
    # over-releases the semaphore.
    gen_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    gen_owner: str | None = None
    gen_permit: bool = False
    gen_recoveries: int = 0
    gen_queued: dict[str, QueuedRequest] = field(default_factory=dict)


class ModelPoolSupervisor:
    """Compatibility facade plus a process-isolated multi-model pool.

    The old supervisor remains the unit of runtime interaction.  Pool mode
    composes several of those units and owns only scheduling, admission and
    lifetime.  Disabling the feature therefore returns execution to the exact
    v1.6.1 implementation instead of maintaining two subtly different worker
    protocols.
    """

    def __init__(self, root: Path, settings):
        self.root = root
        self.settings = settings
        self._legacy = SingleWorkerSupervisor(root, settings)
        self._slots: dict[str, PoolSlot] = {}
        self._primary_model_id: str | None = None
        self._pool_lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        # Cross-model concurrent generation (v1.7.0).  concurrency == 1 keeps the
        # pre-v1.7.0 behaviour: a single permit means exactly one generation at a
        # time across the whole pool, identical to the old global lock.  The
        # value is latched for this coordinator lifetime, like `enabled`.
        self._gen_concurrency = max(1, min(8, int(
            self.settings.data.get("models", {}).get("pool", {}).get(
                "generationConcurrency", 2))))
        self._gen_slots = asyncio.Semaphore(self._gen_concurrency)
        self._gen_active_lanes = 0
        self._request_workers: dict[str, SingleWorkerSupervisor] = {}
        self._reaper_task: asyncio.Task | None = None
        self.maintenance_engines: set[str] = set()
        # Process topology is latched for this coordinator lifetime. Applying
        # this toggle live could strand one side's workers.
        self._pool_enabled = bool(
            self.settings.data.get("models", {}).get("pool", {}).get("enabled", True)
        )
        self._reap_pool_orphans()

    @property
    def enabled(self) -> bool:
        return self._pool_enabled

    @property
    def loaded(self) -> dict | None:
        if not self.enabled:
            return self._legacy.loaded
        if self._legacy.loaded:
            return self._legacy.loaded
        slot = self._primary_slot()
        if slot and slot.state == "ready":
            return slot.worker.loaded
        ready = next((item for item in self._slots.values() if item.state == "ready"), None)
        return ready.worker.loaded if ready else None

    @property
    def loading(self) -> dict | None:
        if not self.enabled:
            return self._legacy.loading
        slot = next((item for item in self._slots.values() if item.state == "loading"), None)
        return slot.worker.loading if slot else None

    @property
    def active_requests(self) -> dict:
        if not self.enabled:
            return self._legacy.active_requests
        result = {}
        for slot in self._slots.values():
            result.update(slot.worker.active_requests)
        return result

    @property
    def queued_requests(self) -> dict:
        if not self.enabled:
            return self._legacy.queued_requests
        result: dict = {}
        for slot in self._slots.values():
            result.update(slot.gen_queued)
            result.update(slot.worker.queued_requests)
        return result

    def _any_generation_busy(self) -> bool:
        return (self._gen_active_lanes > 0
                or any(slot.gen_lock.locked() for slot in self._slots.values()))

    class _LaneLockView:
        """Read-only ``.locked()`` shim for legacy callers of ``generation_lock``."""

        def __init__(self, pool: "ModelPoolSupervisor"):
            self._pool = pool

        def locked(self) -> bool:
            return self._pool._any_generation_busy()

    @property
    def generation_lock(self):
        if not self.enabled:
            return self._legacy.generation_lock
        return ModelPoolSupervisor._LaneLockView(self)

    @property
    def process(self):
        slot = self._primary_slot()
        return slot.worker.process if slot else self._legacy.process

    @property
    def socket_dir(self) -> Path:
        return self._legacy.socket_dir

    @property
    def socket_path(self):
        slot = self._primary_slot()
        return slot.worker.socket_path if slot else self._legacy.socket_path

    @property
    def engine(self):
        slot = self._primary_slot()
        return slot.worker.engine if slot else self._legacy.engine

    def _primary_slot(self) -> PoolSlot | None:
        if not self.enabled or not self._primary_model_id:
            return None
        replicas = self._replica_slots(self._primary_model_id)
        # Prefer a ready replica; fall back to replica 0 while it is still
        # loading so `loading`/`process`/`engine` keep reporting during a load.
        return next((slot for slot in replicas if slot.state == "ready"),
                    replicas[0] if replicas else None)

    def _pool_settings(self) -> dict:
        return self.settings.data.get("models", {}).get("pool", {})

    def _profile(self, model_id: str) -> dict:
        for profile in self._pool_settings().get("profiles", []):
            if profile.get("modelId") == model_id:
                return profile
        return {}

    # --- same-model replicas (v1.8.0) ---------------------------------------

    @staticmethod
    def _slot_key(model_id: str, replica_index: int) -> str:
        # Replica 0 keeps the bare model id as its key so every pre-v1.8.0 code
        # path and test that does `_slots[model_id]` is byte-identical when no
        # extra replicas are configured.
        return model_id if replica_index == 0 else f"{model_id}#{replica_index}"

    def _replica_slots(self, model_id: str) -> list[PoolSlot]:
        """Every slot for this model id, ordered by replica index."""
        return sorted(
            (slot for slot in self._slots.values()
             if str(slot.model.get("id", "")) == model_id),
            key=lambda slot: slot.replica_index,
        )

    def _desired_replicas(self, model_id: str) -> int:
        """How many copies of this model the config asks to keep resident.

        Only a pinned profile can raise it above 1, and the pool-wide
        ``maxReplicasPerModel`` is the hard ceiling. ``replicas == 1`` (the
        default for every model) leaves the pool byte-identical to v1.7.x.
        """
        settings = self._pool_settings()
        ceiling = max(1, int(settings.get("maxReplicasPerModel", 2)))
        try:
            requested = int(self._profile(model_id).get("replicas", 1) or 1)
        except (TypeError, ValueError):
            requested = 1
        return max(1, min(requested, ceiling))

    def _pick_replica(self, model_id: str) -> PoolSlot | None:
        """Choose the replica a new generation for this model should run on.

        Prefers an idle replica (keeps one worker's prompt cache warm when
        traffic is light); otherwise the least-loaded one. Returns None when no
        replica of this model is ready.
        """
        ready = [slot for slot in self._replica_slots(model_id)
                 if slot.state == "ready" and slot.worker.loaded]
        if not ready:
            return None
        idle = [slot for slot in ready
                if not slot.gen_lock.locked() and not slot.gen_queued]
        if idle:
            # Warmest cache first: the most recently released idle replica.
            return max(idle, key=lambda slot: slot.last_released_at)
        return min(ready, key=lambda slot: len(slot.gen_queued)
                   + (1 if slot.gen_lock.locked() else 0))

    def _per_model_limit(self, model: dict) -> int:
        profile = self._profile(str(model.get("id", "")))
        value = profile.get("maxMemoryGB", self._pool_settings().get("defaultPerModelMaxGB", 32))
        return int(float(value) * GIB)

    @staticmethod
    def _physical_memory() -> int:
        with contextlib.suppress(AttributeError, OSError, ValueError):
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        return 0

    def _global_budget(self) -> int:
        physical = self._physical_memory()
        settings = self._pool_settings()
        ratio = float(settings.get("totalMemoryRatio", 0.75))
        reserve = int(float(settings.get("minimumSystemReserveGB", 4)) * GIB)
        return max(0, min(int(physical * ratio), physical - reserve))

    @staticmethod
    def _host_capacity() -> tuple[int, int]:
        """Return reclaimable bytes and the macOS pressure level.

        This is deliberately independent of every worker. A worker's MLX
        counters cannot see another worker or another application.
        """
        available = 0
        pressure = 0
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            output = subprocess.run(["/usr/bin/vm_stat"], capture_output=True,
                                    text=True, timeout=5).stdout
            wanted = ("Pages free:", "Pages inactive:", "Pages speculative:",
                      "Pages purgeable:")
            pages = 0
            for line in output.splitlines():
                if line.startswith(wanted):
                    pages += int(line.rsplit(maxsplit=1)[-1].rstrip("."))
            available = pages * page_size
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        try:
            output = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True, text=True, timeout=5).stdout
            pressure = int(output.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return available, pressure

    @staticmethod
    def _cold_estimate(model: dict) -> int:
        # Catalog size is the weight files' on-disk charge.  Unknown runtime
        # overhead and load transients get 35%, with a 512 MiB process floor.
        # The MLX allocator ceiling and RSS watchdog remain the hard backstops.
        weight_bytes = max(0, int(model.get("size_bytes", 0) or 0))
        return max(512 << 20, int(weight_bytes * 1.35) + (512 << 20))

    def _resident_charge(self) -> int:
        return sum(slot.reservation_bytes for slot in self._slots.values()
                   if slot.state in {"loading", "ready", "evicting"})

    async def _admit(self, model: dict) -> tuple[int, list[PoolSlot]]:
        estimate = self._cold_estimate(model)
        per_model = self._per_model_limit(model)
        if estimate > per_model:
            raise MLXBarError(
                "MODEL_MEMORY_LIMIT",
                f"モデルの安全見積もり{estimate / GIB:.1f} GBがモデル上限{per_model / GIB:.1f} GBを超えています",
                409, False,
            )
        budget = self._global_budget()
        if budget <= 0 or estimate > budget:
            raise MLXBarError("MEMORY_BUDGET_EXCEEDED",
                              "モデルを安全にロードできる全体メモリ予算がありません", 503, True)
        available, pressure = await asyncio.to_thread(self._host_capacity)
        reserve = int(float(self._pool_settings().get("minimumSystemReserveGB", 4)) * GIB)
        if pressure >= 2:
            raise MLXBarError("MEMORY_PRESSURE",
                              "macOSがメモリ逼迫を報告しているため新しいモデルをロードしません", 503, True)
        if available > 0 and available < estimate + reserve:
            raise MLXBarError("MEMORY_BUDGET_EXCEEDED",
                              "現在の空きメモリでは安全余白を残してロードできません", 503, True)
        candidates = sorted(
            (slot for slot in self._slots.values()
             if slot.state == "ready" and slot.leases == 0
             and not slot.keep_loaded and not slot.session_pinned),
            key=lambda item: item.last_released_at,
        )
        evict: list[PoolSlot] = []
        resident = self._resident_charge()
        maximum = int(self._pool_settings().get("maxResidentModels", 2))
        while candidates and (resident + estimate > budget
                              or len(self._slots) - len(evict) >= maximum):
            victim = candidates.pop(0)
            evict.append(victim)
            resident -= victim.reservation_bytes
        if resident + estimate > budget or len(self._slots) - len(evict) >= maximum:
            raise MLXBarError("MEMORY_BUDGET_EXCEEDED",
                              "使用中または固定中のモデルを残したまま安全にロードできません", 503, True)
        return min(per_model, max(estimate, 1 << 30)), evict

    async def load(self, model: dict, engine: str | None = None, *, pin: bool = True) -> dict:
        if not self.enabled:
            return await self._legacy.load(model, engine)
        model_id = str(model.get("id", ""))
        if not model_id:
            raise MLXBarError("MODEL_INCOMPATIBLE", "モデルIDがありません", 409)
        chosen = engine or model.get("engine")
        if chosen in self.maintenance_engines:
            raise MLXBarError("ENGINE_BUSY", "ランタイム切替中です", 409, True)
        if chosen == "lm-studio":
            # LM Studio owns a separate process and does not expose a stable
            # byte-accurate reservation API over REST. Never pretend its
            # instance is covered by the native MLX pool budget: drain native
            # workers and delegate to LM Studio's own guardrails instead.
            async with self._load_lock:
                if self.active_requests or self.queued_requests:
                    raise MLXBarError("ENGINE_BUSY", "生成中はLM Studioへ切り替えられません", 409, True)
                await self._unload_native_slots()
                result = await self._legacy.load(model, chosen)
                self._primary_model_id = str(model.get("id", ""))
                return result
        self._ensure_reaper()
        # An explicit load (GUI / CLI / preload) brings every configured replica
        # up now. An API autoload only needs one copy resident to answer -- the
        # reaper tops a pinned model up to its replica count in the background,
        # so a request never pays the extra admission cost.
        already = len(self._replica_slots(model_id))
        desired = max(1, already, self._desired_replicas(model_id) if pin else 1)
        result: dict | None = None
        for replica_index in range(desired):
            try:
                result = await self._load_replica(model, engine, model_id, replica_index, pin=pin)
            except MLXBarError:
                if replica_index == 0 or result is None:
                    raise
                # A replica beyond the first failing (memory budget, pressure)
                # is not fatal: the model is already usable on the replicas that
                # did load. The reaper will retry the shortfall when room frees.
                LOGGER.warning("Could not load replica %d of %s; keeping %d replica(s)",
                               replica_index, model_id, replica_index)
                break
        self._primary_model_id = model_id
        return result

    async def _load_replica(self, model: dict, engine: str | None, model_id: str,
                            replica_index: int, *, pin: bool) -> dict:
        """Ensure one copy (replica) of ``model`` is resident. Singleflight per
        replica; replica 0 is byte-identical to the pre-v1.8.0 single slot."""
        slot_key = self._slot_key(model_id, replica_index)
        async with self._pool_lock:
            existing = self._slots.get(slot_key)
            if existing:
                if pin:
                    existing.session_pinned = True
                future = existing.load_future
                if existing.state == "ready":
                    self._primary_model_id = model_id
                    return existing.worker.loaded or existing.model
            else:
                future = None
        if future is not None:
            result = await asyncio.shield(future)
            self._primary_model_id = model_id
            return result

        async with self._load_lock:
            victims: list[PoolSlot] = []
            if self._legacy.loaded:
                await self._legacy.unload()
            async with self._pool_lock:
                existing = self._slots.get(slot_key)
                if existing and existing.state == "ready":
                    if pin:
                        existing.session_pinned = True
                    self._primary_model_id = model_id
                    return existing.worker.loaded or existing.model
                if existing and existing.load_future is not None:
                    future = existing.load_future
                else:
                    reservation, victims = await self._admit(model)
                    for victim in victims:
                        victim.state = "evicting"
                    loop = asyncio.get_running_loop()
                    future = loop.create_future()
                    digest = hashlib.sha256(model_id.encode()).hexdigest()[:12]
                    instance_key = digest if replica_index == 0 else f"{digest}-{replica_index}"
                    worker = SingleWorkerSupervisor(
                        self.root, self.settings, instance_key=instance_key,
                        memory_limit_bytes=reservation, reap_orphans=False,
                    )
                    profile = self._profile(model_id)
                    existing = PoolSlot(
                        model=dict(model), worker=worker, reservation_bytes=reservation,
                        replica_index=replica_index,
                        keep_loaded=bool(profile.get("keepLoaded", False)),
                        session_pinned=pin, last_released_at=time.monotonic(), load_future=future,
                    )
                    self._slots[slot_key] = existing
            if future.done():
                return future.result()
            for victim in victims:
                await self._evict_slot(victim)
            try:
                result = await existing.worker.load(model, engine)
                applied_limit = ((result.get("capabilities") or result).get("memoryLimits") or {}).get(
                    "set_memory_limit")
                if applied_limit != existing.worker.memory_limit_bytes:
                    raise MLXBarError(
                        "RUNTIME_MEMORY_LIMIT_UNAVAILABLE",
                        "このランタイムはモデル単位のMLXメモリ上限を確認できないためpoolでは使用しません",
                        409, False,
                    )
                memory = {}
                with contextlib.suppress(Exception):
                    memory = (await existing.worker._call("memory", {}, timeout=5)).get("memory", {})
                observed = max(int(memory.get("process_rss_bytes", 0)),
                               int(memory.get("active_bytes", 0)) + int(memory.get("cache_bytes", 0)))
                if observed > self._per_model_limit(model):
                    raise MLXBarError("MODEL_MEMORY_LIMIT",
                                      "ロード後の実測メモリがモデル上限を超えました", 503, True)
                if observed > existing.reservation_bytes:
                    raise MLXBarError(
                        "MEMORY_BUDGET_EXCEEDED",
                        "ロード後の実測値が事前に確保した安全予約を超えました",
                        503, True,
                    )
                if self._resident_charge() > self._global_budget():
                    raise MLXBarError("MEMORY_BUDGET_EXCEEDED",
                                      "ロード後の実測値が全体メモリ予算を超えました", 503, True)
                existing.state = "ready"
                existing.last_released_at = time.monotonic()
                existing.load_future = None
                self._primary_model_id = model_id
                if not future.done():
                    future.set_result(result)
                return result
            except BaseException as exc:
                await existing.worker.shutdown()
                async with self._pool_lock:
                    if self._slots.get(slot_key) is existing:
                        self._slots.pop(slot_key, None)
                if not future.done():
                    future.set_exception(exc)
                # The caller receives the original exception; retrieving it
                # here prevents an unobserved-future warning when there were no
                # concurrent waiters.
                with contextlib.suppress(BaseException):
                    future.exception()
                raise

    async def load_for_api(self, model: dict, engine: str | None = None) -> dict:
        return await self.load(model, engine, pin=False)

    def find_loaded_model(self, requested: str) -> dict | None:
        if not self.enabled:
            loaded = self._legacy.loaded
            return loaded if loaded and self._matches(loaded, requested) else None
        if self._legacy.loaded and self._matches(self._legacy.loaded, requested):
            return self._legacy.loaded
        for slot in self._slots.values():
            loaded = slot.worker.loaded
            if slot.state == "ready" and loaded and self._matches(loaded, requested):
                self._primary_model_id = str(loaded.get("id", ""))
                return loaded
        return None

    def loaded_models(self) -> list[dict]:
        if not self.enabled:
            return [self._legacy.loaded] if self._legacy.loaded else []
        result = [slot.worker.loaded for slot in self._slots.values()
                  if slot.state == "ready" and slot.worker.loaded]
        if self._legacy.loaded:
            result.append(self._legacy.loaded)
        return result

    @staticmethod
    def _matches(model: dict, requested: str) -> bool:
        value = requested.strip().casefold()
        if value.startswith("openai/"):
            value = value[7:]
        candidates = {str(model.get(key, "")).casefold()
                      for key in ("id", "name", "provider_key")}
        return value in candidates or requested.strip().casefold() in {
            "loaded", "current_model", "local", "x", "openai/x"
        }

    async def _evict_slot(self, slot: PoolSlot) -> None:
        model_id = str(slot.model.get("id", ""))
        slot_key = self._slot_key(model_id, slot.replica_index)
        if slot.leases:
            slot.state = "ready"
            return
        with contextlib.suppress(Exception):
            await slot.worker.unload()
        await slot.worker.shutdown()
        async with self._pool_lock:
            if self._slots.get(slot_key) is slot:
                self._slots.pop(slot_key, None)
            # Only drop the primary pointer when no replica of it survives.
            if self._primary_model_id == model_id and not self._replica_slots(model_id):
                self._primary_model_id = None

    async def unload(self) -> dict:
        if not self.enabled:
            return await self._legacy.unload()
        async with self._load_lock:
            slots = list(self._slots.values())
            for slot in slots:
                if slot.leases:
                    raise MLXBarError("ENGINE_BUSY", "使用中のモデルは解放できません", 409, True)
            for slot in slots:
                slot.state = "evicting"
                await self._evict_slot(slot)
            external = int(bool(self._legacy.loaded))
            if self._legacy.loaded:
                await self._legacy.unload()
            return {"state": "unloaded", "count": len(slots) + external}

    async def unload_native(self) -> dict:
        async with self._load_lock:
            return await self._unload_native_slots()

    async def _unload_native_slots(self) -> dict:
        slots = list(self._slots.values())
        for slot in slots:
            if slot.leases:
                raise MLXBarError("ENGINE_BUSY", "使用中のモデルは解放できません", 409, True)
        for slot in slots:
            slot.state = "evicting"
            await self._evict_slot(slot)
        return {"state": "unloaded", "count": len(slots)}

    async def unload_model(self, model_id: str, *, force: bool = False) -> dict:
        """Free one resident model without disturbing the others.

        The v1.6.x API only had ``unload`` (every model) and per-engine unload;
        a multi-model workflow needs to drop one pinned model at a time.
        """
        if not self.enabled:
            if self._legacy.loaded and str(self._legacy.loaded.get("id", "")) == model_id:
                return await self._legacy.unload()
            return {"state": "unloaded", "count": 0}
        async with self._load_lock:
            if self._legacy.loaded and str(self._legacy.loaded.get("id", "")) == model_id:
                await self._legacy.unload()
                if self._primary_model_id == model_id:
                    self._primary_model_id = None
                return {"state": "unloaded", "count": 1}
            slots = self._replica_slots(model_id)
            if not slots:
                return {"state": "unloaded", "count": 0}
            if any(slot.leases for slot in slots) and not force:
                raise MLXBarError("ENGINE_BUSY", "使用中のモデルは解放できません", 409, True)
            for slot in slots:
                if slot.leases and force:
                    # Cancel only this model's generations, not the whole pool.
                    for queued in list(slot.gen_queued.values()):
                        queued.cancel_requested.set()
                    for req_id, worker in list(self._request_workers.items()):
                        if worker is slot.worker:
                            with contextlib.suppress(Exception):
                                await worker.cancel(req_id)
            for slot in slots:
                slot.state = "evicting"
                slot.session_pinned = False
                await self._evict_slot(slot)
            return {"state": "unloaded", "count": len(slots)}

    async def unload_engine(self, engine: str) -> dict:
        async with self._load_lock:
            targets = [slot for slot in self._slots.values()
                       if (slot.worker.loaded or slot.model).get("engine") == engine]
            for slot in targets:
                if slot.leases:
                    raise MLXBarError("ENGINE_BUSY", "使用中のモデルがあります", 409, True)
            for slot in targets:
                slot.state = "evicting"
                await self._evict_slot(slot)
            return {"state": "unloaded", "count": len(targets)}

    def snapshot_resident(self, engine: str) -> list[dict]:
        if not self.enabled:
            loaded = self._legacy.loaded
            return [dict(loaded)] if loaded and loaded.get("engine") == engine else []
        # One entry per distinct model id: reload_resident() calls self.load(),
        # which re-creates every configured replica, so snapshotting per replica
        # would multiply the reload.
        result: list[dict] = []
        seen: set[str] = set()
        for slot in self._replica_slots_all():
            loaded = slot.worker.loaded or slot.model
            model_id = str(slot.model.get("id", ""))
            if loaded.get("engine") == engine and model_id not in seen:
                seen.add(model_id)
                result.append({"model": dict(slot.model), "engine": engine,
                               "sessionPinned": slot.session_pinned})
        return result

    def _replica_slots_all(self) -> list[PoolSlot]:
        return sorted(self._slots.values(),
                      key=lambda slot: (str(slot.model.get("id", "")), slot.replica_index))

    async def reload_resident(self, snapshots: list[dict], engine: str) -> list[dict]:
        results = []
        for snapshot in snapshots:
            model = snapshot.get("model", snapshot)
            results.append(await self.load(model, engine,
                                           pin=bool(snapshot.get("sessionPinned", False))))
        return results

    def _per_generation_headroom(self, slot: PoolSlot) -> int:
        configured = float(self._pool_settings().get("perGenerationHeadroomGB", 0) or 0)
        if configured > 0:
            return int(configured * GIB)
        # A conservative default: KV cache + activation peak for one generation
        # is a fraction of the model's own reservation.
        return min(int(self._per_model_limit(slot.model) * 0.15), 2 * GIB)

    def _admit_concurrent(self, slot: PoolSlot) -> bool:
        """Would starting one more generation lane keep the combined peak safe?

        A lone generation is always allowed -- that is exactly what every
        release before v1.7.0 did.  Only the second and later concurrent lanes
        are held to the memory head-room and per-model concurrency limits.
        """
        if self._gen_active_lanes == 0:
            return True
        if self._gen_concurrency <= 1:
            return False
        budget = self._global_budget()
        if budget <= 0:
            return False
        charge = self._resident_charge()
        for other in self._slots.values():
            if other is slot:
                continue
            if other.gen_owner is not None or other.worker.active_requests:
                charge += self._per_generation_headroom(other)
        charge += self._per_generation_headroom(slot)
        return charge <= budget

    async def _concurrent_start_ok(self, slot: PoolSlot) -> bool:
        if self._gen_active_lanes == 0:
            return True
        if not self._admit_concurrent(slot):
            return False
        _, pressure = await asyncio.to_thread(self._host_capacity)
        return pressure < 2

    async def _acquire_lane(self, slot: PoolSlot, request_id: str) -> None:
        """Serialise this model, then take one pool-wide concurrency permit.

        Robust to cancellation while waiting for the permit: the model lane lock
        grabbed first is rolled back so a cancelled waiter never strands it.
        """
        await slot.gen_lock.acquire()
        slot.gen_owner = request_id
        raw_permit = False
        try:
            while True:
                await self._gen_slots.acquire()
                raw_permit = True
                # Holding a raw permit is not enough: re-check that adding this
                # lane keeps the combined memory peak and pressure safe.  If
                # not, hand the permit back and wait for a lane to free.  Rare
                # -- only under the head-room guard -- and bounded by the queue
                # timeout, so a short poll is acceptable here.
                if await self._concurrent_start_ok(slot):
                    break
                self._gen_slots.release()
                raw_permit = False
                await asyncio.sleep(0.05)
        except BaseException:
            if raw_permit:
                self._gen_slots.release()
            if slot.gen_owner == request_id:
                slot.gen_owner = None
                if slot.gen_lock.locked():
                    slot.gen_lock.release()
            raise
        self._gen_active_lanes += 1
        slot.gen_permit = True

    def _release_lane(self, slot: PoolSlot, request_id: str) -> bool:
        if slot.gen_owner != request_id:
            return False
        slot.gen_owner = None
        if slot.gen_lock.locked():
            slot.gen_lock.release()
        if slot.gen_permit:
            slot.gen_permit = False
            self._gen_slots.release()
            self._gen_active_lanes = max(0, self._gen_active_lanes - 1)
        return True

    def _recover_lane(self, slot: PoolSlot, source: str) -> bool:
        """Release a lane whose owner vanished after an interrupted stream.

        Mirrors ``WorkerSupervisor._recover_orphaned_generation_slot`` but per
        model.  The pool's pre-v1.7.0 queue had no equivalent, so a client that
        disconnected mid-handshake could pin a lane until process exit.
        """
        if not slot.gen_lock.locked():
            if (slot.gen_owner is not None
                    and slot.gen_owner not in slot.worker.active_requests
                    and slot.gen_owner not in slot.gen_queued):
                slot.gen_owner = None
            return False
        owner = slot.gen_owner
        if owner in slot.worker.active_requests or owner in slot.gen_queued:
            return False
        # An unowned active request could still be mid-generation; keep this
        # lane serialised and let that request's own finalizer finish first.
        if slot.worker.active_requests:
            return False
        slot.gen_owner = None
        if slot.gen_lock.locked():
            slot.gen_lock.release()
        if slot.gen_permit:
            slot.gen_permit = False
            self._gen_slots.release()
            self._gen_active_lanes = max(0, self._gen_active_lanes - 1)
        slot.gen_recoveries += 1
        LOGGER.error("Recovered orphaned pool generation lane model=%s source=%s",
                     slot.model.get("id"), source)
        return True

    async def generate_for_model(self, model_id: str, prompt, images: list[str], options: dict,
                                 request_id: str | None = None, image_root: Path | None = None):
        if not self.enabled:
            async for event in self._legacy.generate(prompt, images, options, request_id, image_root):
                yield event
            return
        if self._legacy.loaded and str(self._legacy.loaded.get("id", "")) == model_id:
            # Native residents are drained before entering this provider path,
            # so the legacy supervisor's established FIFO/cancel contract is
            # the only generation lock needed here.
            async for event in self._legacy.generate(prompt, images, options, request_id, image_root):
                yield event
            return
        slot = self._pick_replica(model_id)
        if slot is None:
            raise MLXBarError("MODEL_NOT_LOADED", "要求されたモデルがロードされていません", 409, True)
        request_id = request_id or hashlib.sha256(os.urandom(16)).hexdigest()
        self._recover_lane(slot, "new_request")
        slot.leases += 1
        queued = None
        acquire_task = None
        cancel_task = None
        try:
            limits = self.settings.data["generation"]
            # Fast path only when the whole pool is idle: with no active lane,
            # `_acquire_lane` runs to completion without hitting an await point,
            # so there is no window for two callers to both see it as free.
            # Every concurrent start goes through the queue loop below, which
            # keeps emitting heartbeats even if `_acquire_lane` blocks on the
            # semaphore or the memory guard.
            must_wait = (slot.gen_lock.locked() or slot.gen_queued
                         or self._gen_active_lanes > 0)
            if must_wait:
                total_queued = sum(len(item.gen_queued) for item in self._slots.values())
                if total_queued >= limits.get("maxQueuedRequests", 16):
                    raise MLXBarError("QUEUE_FULL", "生成待ちが上限に達しています", 429, True)
                queued = QueuedRequest(asyncio.Event(), time.monotonic())
                slot.gen_queued[request_id] = queued
                acquire_task = asyncio.create_task(self._acquire_lane(slot, request_id))
                cancel_task = asyncio.create_task(queued.cancel_requested.wait())
                timeout = limits.get("queueTimeoutSeconds", 3600)
                heartbeat = limits.get("streamHeartbeatSeconds", 10)
                while not acquire_task.done():
                    self._recover_lane(slot, "queue_wait")
                    remaining = timeout - (time.monotonic() - queued.enqueued_at)
                    if remaining <= 0:
                        raise MLXBarError("QUEUE_TIMEOUT", "生成待ち時間が上限を超えました", 429, True)
                    done, _ = await asyncio.wait({acquire_task, cancel_task},
                                                 timeout=min(heartbeat, remaining),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if cancel_task in done:
                        yield {"type": "completed", "finish_reason": "cancelled"}
                        return
                    if acquire_task not in done:
                        yield {"type": "queue", "state": "waiting",
                               "position": list(slot.gen_queued).index(request_id) + 1,
                               "waited_seconds": round(time.monotonic() - queued.enqueued_at, 1)}
                await acquire_task
                # No longer waiting: stop counting this request as queued the
                # moment it owns a lane, matching WorkerSupervisor.generate.
                slot.gen_queued.pop(request_id, None)
                if queued.cancel_requested.is_set():
                    yield {"type": "completed", "finish_reason": "cancelled"}
                    return
            else:
                await self._acquire_lane(slot, request_id)
            self._request_workers[request_id] = slot.worker
            async for event in slot.worker.generate(prompt, images, options, request_id,
                                                    image_root=image_root):
                yield event
        finally:
            if cancel_task:
                cancel_task.cancel()
            if acquire_task and not acquire_task.done():
                acquire_task.cancel()
            await asyncio.gather(*(task for task in (cancel_task, acquire_task) if task),
                                 return_exceptions=True)
            slot.gen_queued.pop(request_id, None)
            self._request_workers.pop(request_id, None)
            # Unconditional: _release_lane is a no-op unless gen_owner matches
            # this request, so it also covers the race where `acquire_task`
            # finished acquiring the lane but the awaiting frame was cancelled
            # before `lane = True` ran.
            self._release_lane(slot, request_id)
            slot.leases = max(0, slot.leases - 1)
            slot.last_released_at = time.monotonic()

    async def generate(self, prompt, images: list[str], options: dict, request_id: str | None = None,
                       image_root: Path | None = None):
        loaded = self.loaded
        if not loaded:
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409)
        async for event in self.generate_for_model(str(loaded.get("id", "")), prompt, images,
                                                   options, request_id, image_root):
            yield event

    def _total_queued(self) -> int:
        return sum(len(slot.gen_queued) for slot in self._slots.values())

    def raise_if_queue_full(self) -> None:
        if not self.enabled:
            return self._legacy.raise_if_queue_full()
        for slot in self._slots.values():
            self._recover_lane(slot, "capacity_check")
        maximum = self.settings.data["generation"].get("maxQueuedRequests", 16)
        total_queued = self._total_queued()
        if (self._any_generation_busy() or total_queued) and total_queued >= maximum:
            raise MLXBarError("QUEUE_FULL", "生成待ちが上限に達しています", 429, True)

    async def cancel(self, request_id: str) -> dict:
        if not self.enabled:
            return await self._legacy.cancel(request_id)
        for slot in self._slots.values():
            queued = slot.gen_queued.get(request_id)
            if queued:
                queued.cancel_requested.set()
                return {"cancelled": True, "queued": True, "forced": False}
        worker = self._request_workers.get(request_id)
        return await worker.cancel(request_id) if worker else {"cancelled": False, "forced": False}

    async def cancel_all(self) -> dict:
        if not self.enabled:
            return await self._legacy.cancel_all()
        queued_count = 0
        for slot in self._slots.values():
            for queued in slot.gen_queued.values():
                queued.cancel_requested.set()
                queued_count += 1
        results = [await worker.cancel(request_id)
                   for request_id, worker in list(self._request_workers.items())]
        return {"cancelled": bool(queued_count or results),
                "queuedCancelled": queued_count, "activeCancelled": len(results),
                "forced": any(result.get("forced", False) for result in results)}

    async def prompt_cache_stats(self) -> dict:
        slot = self._primary_slot()
        return await (slot.worker.prompt_cache_stats() if slot else self._legacy.prompt_cache_stats())

    async def count_tokens(self, model_id: str, messages, options: dict) -> dict:
        """Real tokenizer count for the Anthropic ``count_tokens`` endpoint.

        Routes to any ready replica of the model. Old runtimes without the
        worker RPC fail cleanly rather than returning a fabricated estimate.
        """
        if self.enabled:
            slot = self._pick_replica(model_id) or next(
                (s for s in self._replica_slots(model_id) if s.worker.loaded), None)
            worker = slot.worker if slot else None
        else:
            worker = self._legacy if self._legacy.loaded else None
        if worker is None:
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409)
        params = {"messages": messages, **options}
        try:
            response = await worker._call("count_tokens", params, timeout=30)
        except MLXBarError as exc:
            if exc.code in {"UNKNOWN_METHOD", "PROTOCOL_MISMATCH", "COUNT_TOKENS_UNAVAILABLE"}:
                raise MLXBarError("COUNT_TOKENS_UNAVAILABLE",
                                  "このランタイムはトークン数の計測に対応していません", 503, False) from exc
            raise
        return {"input_tokens": int(response.get("input_tokens", 0))}

    async def clear_memory_prompt_cache(self) -> dict:
        slot = self._primary_slot()
        return await (slot.worker.clear_memory_prompt_cache()
                      if slot else self._legacy.clear_memory_prompt_cache())

    async def clear_disk_prompt_cache(self) -> dict:
        if not self.enabled:
            return await self._legacy.clear_disk_prompt_cache()
        results = []
        for slot in self._slots.values():
            results.append(await slot.worker.clear_disk_prompt_cache())
        return results[-1] if results else await self._legacy.clear_disk_prompt_cache()

    def effective_max_tokens(self) -> int:
        slot = self._primary_slot()
        return (slot.worker if slot else self._legacy).effective_max_tokens()

    def effective_max_prompt_characters(self) -> int:
        slot = self._primary_slot()
        return (slot.worker if slot else self._legacy).effective_max_prompt_characters()

    async def shutdown(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        for slot in list(self._slots.values()):
            await slot.worker.shutdown()
        self._slots.clear()
        await self._legacy.shutdown()

    async def probe_runtime(self, engine: str) -> dict:
        if not self.enabled:
            return await self._legacy.probe_runtime(engine)
        probe = SingleWorkerSupervisor(self.root, self.settings,
                                       instance_key=f"probe-{engine}", reap_orphans=False)
        try:
            return await probe.probe_runtime(engine)
        finally:
            await probe.shutdown()

    async def wait_until_idle(self, timeout: float = 30) -> bool:
        if not self.enabled:
            return await self._legacy.wait_until_idle(timeout)
        try:
            async with asyncio.timeout(timeout):
                while self.active_requests or self.queued_requests or self._any_generation_busy():
                    await asyncio.sleep(0.1)
            return True
        except TimeoutError:
            return False

    def begin_maintenance(self, engine: str) -> None:
        self.maintenance_engines.add(engine)
        self._legacy.begin_maintenance(engine)
        for slot in self._slots.values():
            slot.worker.begin_maintenance(engine)

    def end_maintenance(self, engine: str) -> None:
        self.maintenance_engines.discard(engine)
        self._legacy.end_maintenance(engine)
        for slot in self._slots.values():
            slot.worker.end_maintenance(engine)

    def status(self) -> dict:
        if not self.enabled:
            status = self._legacy.status()
            configured = bool(self._pool_settings().get("enabled", True))
            status["modelPool"] = {
                "enabled": False, "configuredEnabled": configured,
                "restartRequired": configured != self.enabled,
                "residentCount": int(bool(status.get("loadedModel"))),
                "generationConcurrency": 1,
                "activeGenerations": min(1, len(self._legacy.active_requests)),
            }
            status["loadedModels"] = [status["loadedModel"]] if status.get("loadedModel") else []
            return status
        loaded_models = []
        loading = None
        active_generations = 0
        for slot in list(self._slots.values()):
            child = slot.worker.status()
            slot.keep_loaded = bool(self._profile(str(slot.model.get("id", ""))).get(
                "keepLoaded", False))
            if slot.gen_owner is not None or slot.worker.active_requests:
                active_generations += 1
            if child.get("loadedModel"):
                model_id = str(slot.model.get("id", ""))
                entry = {**child["loadedModel"], "poolState": slot.state,
                         "memoryReservationBytes": slot.reservation_bytes,
                         "activeLeases": slot.leases,
                         "replicaIndex": slot.replica_index,
                         "replicaCount": len([s for s in self._slots.values()
                                              if str(s.model.get("id", "")) == model_id]),
                         "laneQueueDepth": len(slot.gen_queued),
                         "laneRecoveries": slot.gen_recoveries,
                         "keepLoaded": slot.keep_loaded or slot.session_pinned,
                         "idleExpiresAt": None if slot.keep_loaded or slot.session_pinned else
                         time.time() + max(0, self._pool_settings().get("idleTTLSeconds", 900)
                                           - (time.monotonic() - slot.last_released_at))}
                # Per-replica live generation rate, straight from the same
                # `_live_generation()` the single-model header already uses. The
                # GUI shows one row per distinct model (replicas collapse), so an
                # aggregate across this model's replicas is folded in below.
                rate = child.get("generationTokensPerSecond")
                if isinstance(rate, (int, float)) and rate > 0:
                    entry["generationTokensPerSecond"] = round(float(rate), 1)
                    produced = child.get("generatedTokens")
                    if isinstance(produced, int) and produced > 0:
                        entry["generatedTokens"] = produced
                loaded_models.append(entry)
            loading = loading or child.get("loadingModel")
        # One rate per distinct model: when a model runs multiple replicas, the
        # collapsed GUI row shows the fastest replica's current rate rather than
        # whichever replica happens to sort first.
        rate_by_model: dict[str, float] = {}
        for entry in loaded_models:
            value = entry.get("generationTokensPerSecond")
            if isinstance(value, (int, float)) and value > 0:
                key = str(entry.get("id", ""))
                rate_by_model[key] = max(rate_by_model.get(key, 0.0), float(value))
        for entry in loaded_models:
            key = str(entry.get("id", ""))
            if key in rate_by_model:
                entry["generationTokensPerSecond"] = round(rate_by_model[key], 1)
        external_status = self._legacy.status()
        if external_status.get("loadedModel"):
            loaded_models.append({**external_status["loadedModel"], "poolState": "external",
                                  "memoryReservationBytes": None, "activeLeases": 0,
                                  "keepLoaded": True, "memoryManagedBy": "lm-studio"})
        loading = loading or external_status.get("loadingModel")
        primary = self.loaded
        primary_id = primary.get("id") if primary else None
        primary_descriptor = next((item for item in loaded_models if item.get("id") == primary_id), None)
        # Mirror the single-model header rate for the primary model. The legacy
        # path emits this via `_live_generation()`; the pool path did not, so
        # `generationRateText` never appeared while the pool was enabled.
        header_rate = (primary_descriptor or {}).get("generationTokensPerSecond")
        if not (isinstance(header_rate, (int, float)) and header_rate > 0):
            header_rate = next((item.get("generationTokensPerSecond") for item in loaded_models
                                if isinstance(item.get("generationTokensPerSecond"), (int, float))
                                and item.get("generationTokensPerSecond") > 0), None)
        return {
            "loadedModel": primary_descriptor,
            "loadedModels": loaded_models,
            "generationTokensPerSecond": header_rate,
            "worker": primary.get("engine") if primary else None,
            "loadingModel": loading,
            "workerRunning": bool(loaded_models),
            "activeRequestCount": len(self.active_requests),
            "queuedRequestCount": len(self.queued_requests),
            "oldestQueuedSeconds": round(time.monotonic() - min(
                (item.enqueued_at for slot in self._slots.values()
                 for item in slot.gen_queued.values()), default=time.monotonic())),
            "generationLockState": "active" if self._any_generation_busy() else "idle",
            "generationLockRecoveries": sum(slot.gen_recoveries for slot in self._slots.values()),
            "generationConcurrency": self._gen_concurrency,
            "activeGenerations": active_generations,
            "maintenanceEngines": sorted(self.maintenance_engines),
            "modelPool": {"enabled": True,
                          "configuredEnabled": bool(self._pool_settings().get("enabled", True)),
                          "restartRequired": not bool(self._pool_settings().get("enabled", True)),
                          "residentCount": len(loaded_models),
                          "residentModelCount": len({item.get("id") for item in loaded_models}),
                          "maxResidentModels": self._pool_settings().get("maxResidentModels", 2),
                          "maxReplicasPerModel": self._pool_settings().get("maxReplicasPerModel", 2),
                          "generationConcurrency": self._gen_concurrency,
                          "activeGenerations": active_generations,
                          "reservedBytes": self._resident_charge(),
                          "budgetBytes": self._global_budget()},
        }

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def _reaper_loop(self) -> None:
        while True:
            ttl = int(self._pool_settings().get("idleTTLSeconds", 900))
            await asyncio.sleep(min(30, max(5, ttl // 4)))
            try:
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient status/OS query failure must not permanently
                # disable TTL and pressure recovery.
                LOGGER.exception("Model pool reaper failed")

    async def _reap_once(self) -> int:
        ttl = int(self._pool_settings().get("idleTTLSeconds", 900))
        now = time.monotonic()
        victims = []
        _, pressure = await asyncio.to_thread(self._host_capacity)
        async with self._pool_lock:
            for slot in self._slots.values():
                child = slot.worker.status()
                dead = child.get("workerRunning") is False and not child.get("loadedModel")
                slot.keep_loaded = bool(self._profile(str(slot.model.get("id", ""))).get(
                    "keepLoaded", False))
                expired = now - slot.last_released_at >= ttl
                pressure_victim = pressure >= 4 and slot.leases == 0
                if dead and slot.state == "ready" and slot.leases == 0:
                    slot.state = "evicting"
                    victims.append(slot)
                elif (slot.state == "ready" and slot.leases == 0
                        and not slot.keep_loaded and not slot.session_pinned
                        and (expired or pressure_victim)):
                    slot.state = "evicting"
                    victims.append(slot)
            # Scale down replicas whose configured count was lowered (or whose
            # ceiling dropped). Never touch replica 0, a leased replica, or the
            # last remaining replica; drop the highest-index idle unpinned ones
            # first so a model keeps its warmest slot.
            for model_id in {str(slot.model.get("id", "")) for slot in self._slots.values()}:
                desired = self._desired_replicas(model_id)
                surviving = [s for s in self._replica_slots(model_id)
                             if s.state == "ready" and s not in victims]
                # A configured replica count is a capacity setting, not a per-
                # process pin: replica 0 is always kept, but replicas 1..N are
                # trimmed to `desired` even when the model is session-pinned.
                droppable = [s for s in reversed(surviving)
                             if s.replica_index > 0 and s.leases == 0]
                for slot in droppable:
                    if len(surviving) <= desired:
                        break
                    slot.state = "evicting"
                    victims.append(slot)
                    surviving.remove(slot)
            # Critical pressure overrides keep-loaded for idle models. A
            # keep-loaded profile is a latency preference, never a promise to
            # endanger the rest of the machine.
            if pressure >= 4:
                for slot in self._slots.values():
                    if slot.state == "ready" and slot.leases == 0 and slot not in victims:
                        slot.state = "evicting"
                        victims.append(slot)
            # Enforce live reductions by evicting oldest unpinned residents.
            # Pinned or leased models are never interrupted; admissions remain
            # blocked until the configured budget is valid again.
            resident = self._resident_charge() - sum(slot.reservation_bytes for slot in victims)
            maximum = int(self._pool_settings().get("maxResidentModels", 2))
            candidates = sorted(
                (slot for slot in self._slots.values()
                 if slot.state == "ready" and slot.leases == 0
                 and not slot.keep_loaded and not slot.session_pinned
                 and slot not in victims),
                key=lambda item: item.last_released_at,
            )
            remaining_count = len(self._slots) - len(victims)
            for slot in candidates:
                if resident <= self._global_budget() and remaining_count <= maximum:
                    break
                slot.state = "evicting"
                victims.append(slot)
                resident -= slot.reservation_bytes
                remaining_count -= 1
        for slot in victims:
            await self._evict_slot(slot)
        await self._scale_up_pinned_replicas()
        return len(victims)

    async def _scale_up_pinned_replicas(self) -> None:
        """Top a pinned/kept model up to its configured replica count.

        API autoload only brings one copy up; this is where a model with a
        `replicas > 1` profile grows to full width once memory allows. Purely
        best effort -- admission failures are expected and left for the next
        pass.
        """
        for model_id in {str(slot.model.get("id", "")) for slot in list(self._slots.values())}:
            replicas = self._replica_slots(model_id)
            if not replicas:
                continue
            base = replicas[0]
            if not (base.keep_loaded or base.session_pinned):
                continue
            desired = self._desired_replicas(model_id)
            ready = [s for s in replicas if s.state == "ready"]
            if len(ready) >= desired or any(s.state == "loading" for s in replicas):
                continue
            present = {s.replica_index for s in replicas}
            model = dict(base.model)
            for index in (i for i in range(desired) if i not in present):
                try:
                    await self._load_replica(model, model.get("engine"), model_id, index,
                                             pin=base.session_pinned)
                except MLXBarError:
                    break

    def _reap_pool_orphans(self) -> None:
        control = self.root / "control"
        if not control.exists():
            return
        for manifest in control.glob("worker-*.json"):
            try:
                import json
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                pid = int(payload["pid"])
            except (OSError, ValueError, TypeError, KeyError):
                manifest.unlink(missing_ok=True)
                continue
            if pid > 1 and SingleWorkerSupervisor._is_our_worker(pid):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(pid, sig)
                    for _ in range(20):
                        if not SingleWorkerSupervisor._process_alive(pid):
                            break
                        time.sleep(0.05)
                    if not SingleWorkerSupervisor._process_alive(pid):
                        break
            socket = payload.get("socket")
            if isinstance(socket, str):
                socket_path = Path(socket)
                # The stale manifest is untrusted. Only touch this
                # coordinator's scoped temporary socket directory.
                with contextlib.suppress(OSError, ValueError):
                    if socket_path.parent.resolve() == self.socket_dir.resolve():
                        socket_path.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
