"""v2.10.0: the Runtime page's oMLX setup path (install / start / stop).

Covers the service itself (detection, brew install, start/stop with the
reachability poll), the job plumbing on AppState (one job per action, kinds,
different-action serialization), and the management endpoints the card calls.
Everything runs against fake executables — no Homebrew, no real server.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from fastapi.testclient import TestClient  # noqa: E402

from mlxbar.database import Database  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
import mlxbar.jobs as jobs_module  # noqa: E402
from mlxbar.main import make_management_app  # noqa: E402
from mlxbar.omlx_setup import OMLXSetupService  # noqa: E402
from mlxbar.state import AppState  # noqa: E402

FAKE_OLMX_BODY = textwrap.dedent("""\
    #!/bin/sh
    case "$1" in
      --version) echo "omlx 1.2.3";;
      start) echo "[omlx] started on http://127.0.0.1:8000";;
      stop) echo "[omlx] stopped";;
      *) echo "[omlx] $@";;
    esac
    exit 0
""")


def write_script(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


TERMINAL_STATES = {"completed", "failed", "cancelled"}


async def noop_update(progress, message):
    """async no-op for tests that must exercise real update() await calls."""


def poll_terminal(database, job_id: str, seconds: float = 30.0) -> dict:
    async def run() -> dict:
        deadline = asyncio.get_running_loop().time() + seconds
        job = database.get_job(job_id)
        while asyncio.get_running_loop().time() < deadline:
            job = database.get_job(job_id)
            if job and job["state"] in TERMINAL_STATES:
                return job
            await asyncio.sleep(0.05)
        return database.get_job(job_id)
    return asyncio.run(run())


def run_setup_job(state, action: str, database, seconds: float = 30.0) -> dict:
    """Create the setup job and poll it to a terminal state inside one loop
    (jobs.create needs a running loop, and the task must survive until done)."""
    async def run() -> dict:
        job = state.omlx_setup_job(action)
        deadline = asyncio.get_running_loop().time() + seconds
        current = database.get_job(job["id"])
        while asyncio.get_running_loop().time() < deadline:
            current = database.get_job(job["id"])
            if current and current["state"] in TERMINAL_STATES:
                return current
            await asyncio.sleep(0.05)
        return database.get_job(job["id"])
    return asyncio.run(run())


class FakeWorkers:
    def __init__(self, connected=True, with_provider=True):
        if with_provider:
            async def connect():
                await asyncio.sleep(0)
                return SimpleNamespace(connected=connected)
            self._omlx_provider = SimpleNamespace(connect=connect)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self._home_patch = patch.dict("os.environ", {"HOME": str(self.home)})
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def which(self, overrides):
        return patch("shutil.which", side_effect=lambda name: overrides.get(name))

    NO_BREW = {"found": False, "path": None, "installCommand": "brew install jundot/omlx/omlx"}

    def no_brew(self):
        """Force `found: False` even on a developer Mac that has real Homebrew
        at /opt/homebrew/bin/brew (the service falls back to it)."""
        return patch.object(OMLXSetupService, "brew_status",
                            staticmethod(lambda: dict(self.NO_BREW)))

    def test_probe_finds_home_cli_and_reads_version(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()
        with self.which({"omlx": None}):
            info = asyncio.run(service.probe_cli(force=True))
        self.assertTrue(info["found"])
        self.assertEqual(info["version"], "omlx 1.2.3")

    def test_probe_ignores_non_executable_home_cli(self):
        target = self.home / ".omlx" / "bin" / "omlx"
        target.parent.mkdir(parents=True)
        target.write_text(FAKE_OLMX_BODY)  # no +x
        service = OMLXSetupService()
        with self.which({"omlx": None}):
            info = asyncio.run(service.probe_cli(force=True))
        self.assertFalse(info["found"])

    def test_probe_uses_cache_between_calls(self):
        service = OMLXSetupService()
        with self.which({"omlx": None}):
            asyncio.run(service.probe_cli(force=True))
        with patch.object(OMLXSetupService, "_run_command",
                          side_effect=AssertionError("cached probe re-ran")):
            cli = asyncio.run(service.probe_cli())
        self.assertFalse(cli["found"])

    def test_requirements_shape(self):
        service = OMLXSetupService()
        cli = {"found": False, "path": None, "version": None}
        req = service.requirements(cli)
        self.assertFalse(req["installed"])
        self.assertEqual(req["installCommand"], "brew install jundot/omlx/omlx")
        self.assertIn("github.com/jundot/omlx", req["releasesUrl"])
        self.assertIn("path", req["brew"])

    def test_install_without_brew(self):
        service = OMLXSetupService()
        with self.no_brew(), self.which({"brew": None, "omlx": None}):
            with self.assertRaises(MLXBarError) as raised:
                asyncio.run(service.install(noop_update))
        self.assertEqual(raised.exception.code, "OMLX_BREW_NOT_FOUND")

    def test_install_with_brew_installs_and_probes(self):
        fake_omlx = write_script(self.bin / "omlx", FAKE_OLMX_BODY)
        brew = write_script(self.bin / "brew", textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$1" = "install" ]; then
              mkdir -p "$HOME/.omlx/bin"
              cp "{fake_omlx}" "$HOME/.omlx/bin/omlx"
              chmod +x "$HOME/.omlx/bin/omlx"
              echo "installed"
              exit 0
            fi
            exit 0
        """))
        service = OMLXSetupService()
        messages = []

        async def update(progress, message):
            messages.append((progress, message))

        with self.which({"brew": brew, "omlx": None}):
            result = asyncio.run(service.install(update))
        self.assertTrue(result["installed"])
        self.assertEqual(result["command"], "brew install jundot/omlx/omlx")
        self.assertEqual(result["omlx"]["version"], "omlx 1.2.3")
        self.assertEqual(messages[-1][0], 1.0)
        self.assertTrue((self.home / ".omlx" / "bin" / "omlx").exists())

    def test_install_brew_failure_raises(self):
        brew = write_script(self.bin / "brew", "#!/bin/sh\necho boom >&2\nexit 7\n")
        service = OMLXSetupService()
        with self.which({"brew": brew, "omlx": None}):
            with self.assertRaises(MLXBarError) as raised:
                asyncio.run(service.install(noop_update))
        self.assertEqual(raised.exception.code, "OMLX_COMMAND_FAILED")
        self.assertIn("boom", str(raised.exception))

    def test_start_requires_cli(self):
        service = OMLXSetupService()
        with self.which({"omlx": None}):
            with self.assertRaises(MLXBarError) as raised:
                asyncio.run(service.start_server(Mock()))
        self.assertEqual(raised.exception.code, "OMLX_NOT_INSTALLED")

    def test_start_reaches_configured_server(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()
        probe_calls = []

        async def verify():
            probe_calls.append(1)
            return SimpleNamespace(connected=True)

        async def update(progress, message):
            pass

        with self.which({"omlx": None}):
            result = asyncio.run(service.start_server(update, verify=verify))
        self.assertTrue(result["started"])
        self.assertTrue(result["serverReachable"])
        self.assertEqual(probe_calls, [1])
        self.assertEqual(result["version"], "omlx 1.2.3")

    def test_start_accepts_dict_status(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()

        async def verify():
            return {"connected": True}

        async def update(progress, message):
            pass

        with self.which({"omlx": None}):
            result = asyncio.run(service.start_server(update, verify=verify))
        self.assertTrue(result["serverReachable"])

    def test_start_reports_unreachable_without_failing(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()
        short_poll = patch("mlxbar.omlx_setup.REACHABILITY_POLL_SECONDS", 0.6)
        short_interval = patch("mlxbar.omlx_setup.REACHABILITY_INTERVAL_SECONDS", 0.2)
        with short_poll, short_interval, self.which({"omlx": None}):

            async def verify():
                return SimpleNamespace(connected=False)

            async def update(progress, message):
                pass

            result = asyncio.run(service.start_server(update, verify=verify))
        self.assertTrue(result["started"])
        self.assertFalse(result["serverReachable"])

    def test_start_command_failure(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", """#!/bin/sh
if [ "$1" = "start" ]; then echo "[omlx] bind failed" >&2; exit 3; fi
if [ "$1" = "--version" ]; then echo "omlx 1.2.3"; fi
exit 0
""")
        service = OMLXSetupService()
        with self.which({"omlx": None}):
            with self.assertRaises(MLXBarError) as raised:
                asyncio.run(service.start_server(noop_update, verify=None))
        self.assertEqual(raised.exception.code, "OMLX_COMMAND_FAILED")
        self.assertIn("bind failed", str(raised.exception))

    def test_stop_reports_leftover_reachability(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()

        async def verify():
            return {"connected": True}

        async def update(progress, message):
            pass

        with self.which({"omlx": None}):
            result = asyncio.run(service.stop_server(update, verify=verify))
        self.assertTrue(result["stopped"])
        self.assertTrue(result["serverStillReachable"])

    def test_actions_never_overlap(self):
        """install/start/stop serialize through the service's action lock, so
        a `brew install` racing `omlx start` cannot probe a half-installed
        tap. Either lock-acquisition order is a valid serialization, so the
        assertion is that the two action commands do not overlap — not that
        start wins the lock race (its lock-free initial probe can lose that
        race under suite load)."""
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        service = OMLXSetupService()
        markers: list[tuple[str, float]] = []
        probes = {"start": 0, "stop": 0}
        original = OMLXSetupService._run_command

        async def tracked(self_service, argv, update, **kwargs):
            is_action = len(argv) >= 2 and argv[1] in {"start", "stop"}
            index = None
            if is_action:
                tag = argv[1]
                probes[tag] += 1
                index = probes[tag]
                markers.append((f"{tag}{index}:enter", asyncio.get_running_loop().time()))
            result = await original(self_service, argv, update, **kwargs)
            if is_action:
                markers.append((f"{tag}{index}:exit", asyncio.get_running_loop().time()))
            return result

        async def run():
            with patch.object(OMLXSetupService, "_run_command", tracked):
                first = asyncio.create_task(service.start_server(noop_update))
                await asyncio.sleep(0.05)
                second = asyncio.create_task(service.stop_server(noop_update))
                await asyncio.gather(first, second)

        with self.which({"omlx": None}):
            asyncio.run(run())
        exits = {name.rsplit(":", 1)[0]: stamp
                 for name, stamp in markers if name.endswith(":exit")}
        enters = {name.rsplit(":", 1)[0]: stamp
                  for name, stamp in markers if name.endswith(":enter")}
        self.assertEqual(set(enters), {"start1", "stop1"}, markers)
        # Exclusive possession of the action lock: whichever action command
        # ran second entered only after the first one actually finished.
        overlap = (enters["stop1"] < exits["start1"]
                   and enters["start1"] < exits["stop1"])
        self.assertFalse(overlap, markers)


def bound_state(database, jobs, service, workers) -> SimpleNamespace:
    state = SimpleNamespace(database=database, jobs=jobs, omlx_setup=service,
                            omlx_setup_jobs={}, workers=workers)
    state.omlx_setup_job = types.MethodType(AppState.omlx_setup_job, state)
    state.omlx_connect_probe = types.MethodType(AppState.omlx_connect_probe, state)
    return state


NO_BREW_PAYLOAD = {"found": False, "path": None, "installCommand": "brew install jundot/omlx/omlx"}


def no_brew():
    """`found: False` even on a developer Mac whose /opt/homebrew/bin/brew
    exists (brew_status falls back to it)."""
    return patch.object(OMLXSetupService, "brew_status",
                        staticmethod(lambda: dict(NO_BREW_PAYLOAD)))


class AppStateJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._home_patch = patch.dict("os.environ", {"HOME": str(self.home)})
        self._home_patch.start()
        self.database = Database(self.tmp / "state.sqlite3")
        self.state = bound_state(self.database, jobs_module.JobManager(self.database),
                                 OMLXSetupService(), FakeWorkers(connected=True))
        self.jobs = self.state.jobs

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unsupported_action(self):
        with self.assertRaises(ValueError):
            self.state.omlx_setup_job("reload")

    def test_same_action_dedups_to_one_job(self):
        brew = write_script(self.tmp / "bin" / "brew", """#!/bin/sh
touch "$HOME/.brew-ran"
sleep 0.5
echo installed
exit 0
""")
        overrides = {"brew": brew}

        async def run():
            with patch("shutil.which", side_effect=lambda name: overrides.get(name)):
                job = self.state.omlx_setup_job("install")
                again = self.state.omlx_setup_job("install")
                self.assertEqual(job["id"], again["id"])
                self.assertEqual(job["kind"], "omlx_install")
                # Cancel only once the fake brew is verifiably running: a
                # cancel colliding with the subprocess *spawn* itself races
                # asyncio's transport creation and is not lettable
                # deterministically.
                stamp = self.home / ".brew-ran"
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if stamp.exists():
                        break
                self.assertTrue(stamp.exists(), "fake brew never started")
                cancelled = await self.jobs.cancel(job["id"])
                self.assertEqual(cancelled["state"], "cancelled")

        asyncio.run(run())

    def test_cancel_fallback_records_terminal_state(self):
        """If the cancelling job task never reaches `_run`'s own terminal
        write (cancelled before/around its first step, or unmanaged), the
        JobManager's cancel path itself records 'cancelled', pops the
        bookkeeping, and hands a terminal event to an SSE subscriber."""
        self.database.upsert_job(
            {"id": "ghost", "kind": "omlx_stop", "state": "running",
             "progress": 0.0, "message": "", "result": None, "error": None})

        async def ghost():
            await asyncio.sleep(3600)

        async def run():
            task = asyncio.create_task(ghost())
            self.jobs.tasks["ghost"] = task
            self.jobs.queues["ghost"] = asyncio.Queue()
            final = await self.jobs.cancel("ghost")
            self.assertEqual(final["state"], "cancelled")
            self.assertNotIn("ghost", self.jobs.tasks)
            self.assertNotIn("ghost", self.jobs.queues)

        asyncio.run(run())

    def test_start_job_wires_the_provider_probe(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        with patch("shutil.which", side_effect=lambda name: None):
            final = run_setup_job(self.state, "start", self.database)
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue(final["result"]["started"])
        self.assertTrue(final["result"]["serverReachable"])

    def test_start_job_without_provider_reports_unreachable(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        self.state.workers = SimpleNamespace()  # no provider at all
        short_poll = patch("mlxbar.omlx_setup.REACHABILITY_POLL_SECONDS", 0.4)
        short_interval = patch("mlxbar.omlx_setup.REACHABILITY_INTERVAL_SECONDS", 0.1)
        with patch("shutil.which", side_effect=lambda name: None), short_poll, short_interval:
            final = run_setup_job(self.state, "start", self.database)
        self.assertEqual(final["state"], "completed", final)
        self.assertFalse(final["result"]["serverReachable"])

    def test_stop_job_reports_still_reachable_from_probe(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        with patch("shutil.which", side_effect=lambda name: None):
            final = run_setup_job(self.state, "stop", self.database)
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue(final["result"]["stopped"])
        self.assertTrue(final["result"]["serverStillReachable"])

    def test_failed_start_job_carries_the_error(self):
        write_script(self.home / ".omlx" / "bin" / "omlx", """#!/bin/sh
if [ "$1" = "start" ]; then echo "[omlx] bind failed" >&2; exit 3; fi
if [ "$1" = "--version" ]; then echo "omlx 1.2.3"; fi
exit 0
""")
        with patch("shutil.which", side_effect=lambda name: None):
            final = run_setup_job(self.state, "start", self.database)
        self.assertEqual(final["state"], "failed", final)
        self.assertEqual(final["error"]["code"], "OMLX_COMMAND_FAILED")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._home_patch = patch.dict("os.environ", {"HOME": str(self.home)})
        self._home_patch.start()
        self.database = Database(self.tmp / "state.sqlite3")
        self.state = bound_state(self.database, jobs_module.JobManager(self.database),
                                 OMLXSetupService(), FakeWorkers(connected=True))

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def which_all_none(self):
        return patch("shutil.which", side_effect=lambda name: None)

    def test_requirements_endpoint(self):
        with TestClient(make_management_app(self.state)) as client:
            with self.which_all_none(), no_brew():
                response = client.get("/api/v1/omlx/setup/requirements")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["installed"])
        self.assertFalse(data["brew"]["found"])
        self.assertEqual(data["installCommand"], "brew install jundot/omlx/omlx")

    def test_install_start_stop_cancel_flow(self):
        brew = write_script(self.tmp / "bin" / "brew", """#!/bin/sh
touch "$HOME/.brew-ran"
sleep 3
echo installed
exit 0
""")
        with patch("shutil.which",
                   side_effect=lambda name: brew if name == "brew" else None):
            with TestClient(make_management_app(self.state)) as client:
                job = client.post("/api/v1/omlx/setup/install").json()
                self.assertEqual(job["kind"], "omlx_install")
                again = client.post("/api/v1/omlx/setup/install").json()
                self.assertEqual(job["id"], again["id"])
                import time
                stamp = self.home / ".brew-ran"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not stamp.exists():
                    time.sleep(0.05)
                self.assertTrue(stamp.exists(), "fake brew never started")
                result = client.post(f"/api/v1/omlx/setup/jobs/{job['id']}/cancel")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["state"], "cancelled")

    def test_start_endpoint_completes_with_provider_probe(self):
        import time
        write_script(self.home / ".omlx" / "bin" / "omlx", FAKE_OLMX_BODY)
        with self.which_all_none():
            with TestClient(make_management_app(self.state)) as client:
                job = client.post("/api/v1/omlx/setup/start").json()
                deadline = time.monotonic() + 30
                final = self.database.get_job(job["id"])
                while time.monotonic() < deadline and final["state"] not in TERMINAL_STATES:
                    time.sleep(0.05)
                    final = self.database.get_job(job["id"])
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue(final["result"]["serverReachable"])

    def test_cancel_unknown_job(self):
        with TestClient(make_management_app(self.state)) as client:
            result = client.post("/api/v1/omlx/setup/jobs/nope/cancel")
        self.assertEqual(result.status_code, 404)

    def test_cancel_wrong_kind_job(self):
        self.database.upsert_job(
            {"id": "scan1", "kind": "model_scan", "state": "running",
             "progress": 0.0, "message": "", "result": None, "error": None})
        with TestClient(make_management_app(self.state)) as client:
            result = client.post("/api/v1/omlx/setup/jobs/scan1/cancel")
        self.assertEqual(result.status_code, 404)


if __name__ == "__main__":
    unittest.main()
