from __future__ import annotations

import asyncio

from ..errors import MLXBarError


class RuntimeUpdateService:
    def __init__(self, updater, slots, workers, database):
        self.updater = updater
        self.slots = slots
        self.workers = workers
        self.database = database

    async def update_latest(self, engine: str, progress) -> dict:
        await progress(0.02, "最新版を確認中")
        candidate = await self.updater.check(engine)
        if not candidate["updateAvailable"]:
            return {"engine": engine, "updated": False, "reason": "already_latest", "check": candidate}

        old_slot = self.slots.active(engine).get("active")
        loaded = None
        resident = []
        staged = None
        activated = False
        maintenance = False
        try:
            staged = await self.updater.stage(engine, progress, version=candidate["candidateVersion"])
            new_slot = staged["slotId"]
            self.database.add_runtime_history(engine, new_slot, "staged", {"check": candidate, "probe": staged["probe"]})
            self.workers.begin_maintenance(engine)
            maintenance = True
            snapshot = getattr(self.workers, "snapshot_resident", None)
            resident = snapshot(engine) if snapshot else []
            loaded = self.workers.loaded.copy() if self.workers.loaded and self.workers.loaded.get("engine") == engine else None
            if loaded and not resident:
                resident = [{"model": loaded, "engine": engine, "sessionPinned": True}]
            if resident:
                await progress(0.91, "実行中の生成が終わるのを待っています")
                if not await self.workers.wait_until_idle(timeout=30):
                    raise MLXBarError("ENGINE_BUSY", "生成が続いているため切替を中止しました。生成完了後に再実行してください", 409, True)
            await progress(0.92, "新しいランタイムへ切替中")
            if resident:
                unload_engine = getattr(self.workers, "unload_engine", None)
                if unload_engine:
                    await unload_engine(engine)
                else:
                    await self.workers.unload()
            self.slots.activate(engine, new_slot)
            activated = True
            await progress(0.95, "切替後のワーカーを確認中")
            if not self.workers.loaded or callable(getattr(self.workers, "snapshot_resident", None)):
                await self.workers.probe_runtime(engine)
            if resident:
                await progress(0.97, "使用中モデルを再ロード中")
                reload_resident = getattr(self.workers, "reload_resident", None)
                if reload_resident:
                    await reload_resident(resident, engine)
                else:
                    await self.workers.load(loaded, engine)
            result = {"engine": engine, "updated": True, "fromSlot": old_slot,
                      "toSlot": new_slot, "version": staged["probe"].get("version"), "check": candidate}
            result["removedSlots"] = self.slots.cleanup(
                engine, int(getattr(self.workers, "settings", None).data["runtimes"].get("keepSlots", 3))
                if getattr(self.workers, "settings", None) else 3
            )
            self.database.add_runtime_history(engine, new_slot, "activated", result)
            self.workers.end_maintenance(engine)
            maintenance = False
            await progress(1.0, "更新が完了しました")
            return result
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            failed_slot = staged["slotId"] if staged else old_slot or "none"
            rollback_error = None
            if activated:
                try:
                    unload_engine = getattr(self.workers, "unload_engine", None)
                    if unload_engine:
                        await unload_engine(engine)
                    else:
                        await self.workers.unload()
                    self.slots.restore(engine, old_slot)
                    if resident and old_slot:
                        reload_resident = getattr(self.workers, "reload_resident", None)
                        if reload_resident:
                            await reload_resident(resident, engine)
                        else:
                            await self.workers.load(loaded, engine)
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            self.database.add_runtime_history(engine, failed_slot, "cancelled" if cancelled else "failed", {
                "message": "利用者が更新を中止しました" if cancelled else str(exc),
                "rolledBack": activated and rollback_error is None,
                "rollbackError": rollback_error,
            })
            if maintenance:
                self.workers.end_maintenance(engine)
            if rollback_error:
                raise MLXBarError("ROLLBACK_FAILED", f"更新失敗: {exc} / 復元失敗: {rollback_error}", 500) from exc
            if cancelled:
                raise
            if isinstance(exc, MLXBarError):
                raise
            raise MLXBarError("UPDATE_FAILED", str(exc), 409) from exc
