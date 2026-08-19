from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from .database import Database


class JobManager:
    def __init__(self, database: Database):
        self.database = database
        self.database.fail_incomplete_jobs()
        self.queues: dict[str, asyncio.Queue] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def create(self, kind: str, work: Callable[[Callable[..., Awaitable[None]]], Awaitable[object]]) -> dict:
        job_id = str(uuid.uuid4())
        job = {"id": job_id, "kind": kind, "state": "queued", "progress": 0.0,
               "message": "待機中", "result": None, "error": None}
        self.database.upsert_job(job)
        self.queues[job_id] = asyncio.Queue()
        self.tasks[job_id] = asyncio.create_task(self._run(job, work))
        return self.database.get_job(job_id)

    async def _run(self, job: dict, work) -> None:
        async def update(progress: float | None, message: str, state: str = "running") -> None:
            job.update(state=state, progress=progress, message=message)
            self.database.upsert_job(job)
            await self.queues[job["id"]].put({"state": state, "progress": progress, "message": message})

        try:
            await update(0.0, "開始しています")
            result = await work(update)
            job.update(state="completed", progress=1.0, message="完了", result=result)
        except asyncio.CancelledError:
            job.update(state="cancelled", message="利用者が処理を中止しました",
                       error={"code": "JOB_CANCELLED", "message": "処理を中止しました"})
        except Exception as exc:
            job.update(state="failed", message="失敗", error={"code": getattr(exc, "code", "INTERNAL_ERROR"),
                                                                 "message": str(exc)})
        self.database.upsert_job(job)
        await self.queues[job["id"]].put({"state": job["state"], "result": job.get("result"),
                                           "error": job.get("error")})
        self.tasks.pop(job["id"], None)

    async def cancel_all(self) -> None:
        """Cancel every still-running job (e.g. an in-flight runtime install)
        so its subprocess tree is killed via `RuntimeUpdater._terminate`
        instead of being orphaned when the coordinator process exits. Runs
        concurrently so N jobs each needing up to ~5s to kill their process
        group don't add up to N * 5s against the caller's shutdown budget."""
        await asyncio.gather(*(self.cancel(job_id) for job_id in list(self.tasks)),
                             return_exceptions=True)

    async def cancel(self, job_id: str) -> dict | None:
        job = self.database.get_job(job_id)
        if not job:
            return None
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        task = self.tasks.get(job_id)
        if not task:
            return job
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return self.database.get_job(job_id)

    async def events(self, job_id: str):
        job = self.database.get_job(job_id)
        if not job:
            return
        yield job
        if job["state"] in {"completed", "failed", "cancelled"}:
            return
        queue = self.queues.setdefault(job_id, asyncio.Queue())
        while True:
            event = await queue.get()
            yield event
            if event.get("state") in {"completed", "failed", "cancelled"}:
                return
