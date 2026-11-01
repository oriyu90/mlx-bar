"""v4.0.1 / WP-1: a *load window* a test can hold open (test-only helper).

`FakeWorker.load()` already waits on `FakeWorker.load_gate` before returning,
and `ModelPoolSupervisor` publishes its `PoolSlot` **before** awaiting that
load. So with the gate still unset the pool is in exactly the state the v4.0.0
field reports could not be produced (F-01 blocked every load): a slot exists,
its worker has been asked to load, and the load has not finished yet.

Nothing here touches product code -- it only wraps the three lines of
boilerplate (gate, background task, wait-until-published) so the WP-2/WP-3
regression tests read as "while a load is in progress, ...".

Every wait in this module is bounded: a helper that hangs turns a test failure
into a hung test run, which is worse than the bug it is meant to catch.
"""

from __future__ import annotations

import asyncio
import contextlib

from test_model_pool import FakeWorker

# How long a load may take to *publish its slot*. Generous (the pool only does
# a little synchronous work first) but finite.
PUBLISH_TIMEOUT_SECONDS = 5.0
# How long the load may take to finish once the gate is released.
FINISH_TIMEOUT_SECONDS = 10.0


async def wait_for_slot(pool, model_id: str, *, state: str | None = None,
                        timeout: float = PUBLISH_TIMEOUT_SECONDS):
    """Wait until ``pool`` has a slot for ``model_id`` (optionally in ``state``).

    Raises ``AssertionError`` (not a timeout exception) so a failure reads as
    "the pool never published a loading slot", which is the actual bug.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for slot in list(pool._slots.values()):
            if str(slot.model.get("id", "")) != model_id:
                continue
            if state is None or slot.state == state:
                return slot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"slot for {model_id!r} was not published as state={state!r} "
                f"within {timeout}s (slots={list(pool._slots)})")
        await asyncio.sleep(0.01)


class LoadWindow:
    """Handle yielded by :func:`load_in_progress`."""

    def __init__(self, pool, model, gate: asyncio.Event, task: asyncio.Task):
        self.pool = pool
        self.model = model
        self.gate = gate
        self.task = task

    @property
    def model_id(self) -> str:
        return str(self.model.get("id", ""))

    def slot(self):
        for slot in self.pool._slots.values():
            if str(slot.model.get("id", "")) == self.model_id:
                return slot
        return None

    def release(self) -> None:
        """Let the pending load finish (idempotent)."""
        self.gate.set()

    async def finish(self) -> dict:
        """Release the gate and return the completed load result."""
        self.release()
        return await asyncio.wait_for(self.task, FINISH_TIMEOUT_SECONDS)


@contextlib.asynccontextmanager
async def load_in_progress(pool, model: dict, *, release: bool = True):
    """Hold a load open for the duration of the ``async with`` block.

    ``release=True`` (the default) lets the load complete on exit, so the
    fixture leaves no dangling task behind. A test that wants to observe what
    happens when the load *never* finishes can pass ``release=False`` and the
    task is cancelled instead.
    """
    gate = asyncio.Event()
    previous = FakeWorker.load_gate
    FakeWorker.load_gate = gate
    task = asyncio.create_task(pool.load_for_api(dict(model)))
    try:
        await wait_for_slot(pool, str(model.get("id", "")), state="loading")
        window = LoadWindow(pool, model, gate, task)
        yield window
    finally:
        FakeWorker.load_gate = previous
        if release:
            gate.set()
        else:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), FINISH_TIMEOUT_SECONDS)
