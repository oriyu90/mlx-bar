"""Regression tests for the v1.1.0 security and stability fixes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Coordinator"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Workers"))

from mlxbar.api.images import resolve_public_images  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.runtimes.updater import RuntimeUpdater  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app, max_request_bytes  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.supervisor import WorkerSupervisor  # noqa: E402

PNG_PIXEL = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)).decode()


def store(directory: Path) -> SettingsStore:
    return SettingsStore(directory)


# --- Untrusted image references (SSRF / arbitrary file read) -----------------

def test_local_path_image_is_refused_on_the_public_api():
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        secret = Path(directory) / "private.png"
        secret.write_bytes(b"not really a png")
        with pytest.raises(MLXBarError) as error:
            asyncio.run(resolve_public_images([str(secret)], settings))
        assert error.value.status == 422


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1:8080/internal.png",
    "../../../etc/hosts",
])
def test_remote_and_traversal_urls_are_refused_by_default(url):
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        with pytest.raises(MLXBarError):
            asyncio.run(resolve_public_images([url], settings))


def test_private_addresses_stay_refused_even_when_remote_fetch_is_enabled():
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        settings.update({"security": {"allowRemoteImageUrls": True}})
        with pytest.raises(MLXBarError) as error:
            asyncio.run(resolve_public_images(["http://127.0.0.1:9/secret.png"], settings))
        assert "プライベート" in error.value.message


def test_data_uri_image_is_materialised_into_a_private_workspace():
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        paths, workspace = asyncio.run(
            resolve_public_images([f"data:image/png;base64,{PNG_PIXEL}"], settings))
        try:
            assert len(paths) == 1
            resolved = Path(paths[0])
            assert resolved.is_file()
            assert resolved.is_relative_to(workspace.path)
            assert oct(resolved.stat().st_mode)[-3:] == "600"
            assert oct(workspace.path.stat().st_mode)[-3:] == "700"
        finally:
            workspace.cleanup()
        assert not workspace.path.exists()


def test_oversized_data_uri_is_refused_before_decoding():
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        settings.update({"generation": {"maxImageBytes": 1024}})
        payload = base64.b64encode(b"x" * 8192).decode()
        with pytest.raises(MLXBarError) as error:
            asyncio.run(resolve_public_images([f"data:image/png;base64,{payload}"], settings))
        assert error.value.status == 413


def test_generation_rejects_images_outside_the_declared_root():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = store(root)
        supervisor = WorkerSupervisor(root, settings)
        allowed = root / "workspace"
        allowed.mkdir()
        inside = allowed / "ok.png"
        inside.write_bytes(b"x")
        outside = root / "outside.png"
        outside.write_bytes(b"x")

        prompt, images, _ = supervisor._validate_generation("hi", [str(inside)], {}, allowed)
        assert images == [str(inside)]

        with pytest.raises(MLXBarError):
            supervisor._validate_generation("hi", [str(outside)], {}, allowed)


def test_generation_rejects_symlink_escape_from_the_declared_root():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = store(root)
        supervisor = WorkerSupervisor(root, settings)
        allowed = root / "workspace"
        allowed.mkdir()
        target = root / "secret.png"
        target.write_bytes(b"x")
        link = allowed / "escape.png"
        link.symlink_to(target)
        with pytest.raises(MLXBarError):
            supervisor._validate_generation("hi", [str(link)], {}, allowed)


def test_local_gui_path_still_accepts_absolute_image_paths():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        picture = root / "picture.png"
        picture.write_bytes(b"x")
        _, images, _ = supervisor._validate_generation("hi", [str(picture)], {})
        assert images == [str(picture)]


# --- Orphaned worker recovery ----------------------------------------------

def test_orphaned_worker_from_a_previous_run_is_terminated_on_startup():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        settings = store(root)
        # Stand in for a real worker: the reaper only kills a pid whose command
        # line still names one of our worker modules.
        script = "import time\ntime.sleep(120)\n"
        process = subprocess.Popen(
            [sys.executable, "-c", script, "mlx_lm_worker.adapter"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            supervisor = WorkerSupervisor(root, settings)
            socket_dir = supervisor.socket_dir
            socket_dir.mkdir(parents=True, exist_ok=True)
            stale = socket_dir / "mlx-lm-deadbeef.sock"
            stale.touch()
            (root / "control").mkdir(parents=True, exist_ok=True)
            supervisor.manifest_path.write_text(json.dumps(
                {"pid": process.pid, "engine": "mlx-lm", "socket": str(stale)}))

            reaped = supervisor.reap_orphan_worker()

            assert reaped is not None and reaped["pid"] == process.pid
            assert process.wait(timeout=10) is not None
            assert not supervisor.manifest_path.exists()
            assert not stale.exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_reaper_leaves_an_unrelated_process_alone():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            supervisor = WorkerSupervisor(root, store(root))
            (root / "control").mkdir(parents=True, exist_ok=True)
            supervisor.manifest_path.write_text(json.dumps({"pid": process.pid, "engine": "mlx-lm"}))
            assert supervisor.reap_orphan_worker() is None
            time.sleep(0.2)
            assert process.poll() is None, "a pid we did not spawn must not be killed"
        finally:
            process.kill()
            process.wait(timeout=10)


def test_reaper_tolerates_a_missing_or_corrupt_manifest():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        assert supervisor.reap_orphan_worker() is None
        supervisor.manifest_path.write_text("{not json")
        assert supervisor.reap_orphan_worker() is None


def test_socket_directory_is_scoped_per_coordinator_root():
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        one = WorkerSupervisor(Path(first), store(Path(first)))
        two = WorkerSupervisor(Path(second), store(Path(second)))
        assert one.socket_dir != two.socket_dir


# --- Stale "loaded" state after a silent worker death -----------------------

def test_status_forgets_a_model_whose_worker_died_while_idle():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        supervisor.loaded = {"id": "m", "name": "Model", "engine": "mlx-lm"}
        supervisor.process = None
        assert supervisor.status()["loadedModel"] is None


def test_status_keeps_an_lm_studio_model_that_needs_no_worker_process():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        supervisor.loaded = {"id": "m", "name": "Model", "engine": "lm-studio"}
        assert supervisor.status()["loadedModel"]["name"] == "Model"


def test_status_keeps_a_model_while_a_request_is_still_in_flight():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        supervisor.loaded = {"id": "m", "name": "Model", "engine": "mlx-lm"}
        supervisor.active_requests["r"] = object()
        assert supervisor.status()["loadedModel"]["name"] == "Model"


# --- Worker stderr goes to a log file rather than an undrained pipe ---------

def test_worker_startup_failure_is_reported_from_the_log_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        log = supervisor.worker_log_path("mlx-lm")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ModuleNotFoundError: No module named 'mlx'\n")
        assert "ModuleNotFoundError" in supervisor._read_log_tail(log)


def test_log_tail_is_capped_and_survives_a_missing_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        log = supervisor.worker_log_path("mlx-vlm")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"a" * 50_000)
        assert len(supervisor._read_log_tail(log)) <= 4000
        assert supervisor._read_log_tail(root / "does-not-exist.log")


def test_worker_log_is_rotated_once_it_grows_past_the_cap():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        supervisor = WorkerSupervisor(root, store(root))
        log = supervisor.worker_log_path("mlx-lm")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(b"x" * 2_000_000)
        handle = supervisor._open_worker_log("mlx-lm")
        handle.close()
        assert log.stat().st_size == 0
        assert oct(log.stat().st_mode)[-3:] == "600"


# --- Runtime update timeout -------------------------------------------------

def test_runtime_command_times_out_instead_of_running_forever():
    updater = RuntimeUpdater(store=None)

    async def run():
        with pytest.raises(MLXBarError) as error:
            await updater._command(sys.executable, "-c", "import time; time.sleep(60)", timeout=1)
        assert error.value.code == "UPDATE_TIMEOUT"
        assert error.value.retryable

    asyncio.run(run())


def test_runtime_command_still_returns_output_within_its_timeout():
    updater = RuntimeUpdater(store=None)

    async def run():
        output = await updater._command(sys.executable, "-c", "print('done')", timeout=30)
        assert "done" in output

    asyncio.run(run())


# --- Bounded cancellation registry -----------------------------------------

def test_cancellation_registry_is_bounded_and_expires_stale_entries():
    from common.server import CancellationRegistry

    registry = CancellationRegistry(retention=1000, maximum=4)
    for index in range(10):
        registry.add(f"request-{index}")
    assert len(registry) == 4
    assert "request-9" in registry
    assert "request-0" not in registry

    expiring = CancellationRegistry(retention=-1, maximum=16)
    expiring.add("old")
    assert "old" not in expiring
    assert len(expiring) == 0


# --- Settings validation for the new security switch ------------------------

def test_allow_remote_image_urls_defaults_off_and_must_be_boolean():
    with tempfile.TemporaryDirectory() as directory:
        settings = store(Path(directory))
        assert settings.data["security"]["allowRemoteImageUrls"] is False
        with pytest.raises(ValueError):
            settings.update({"security": {"allowRemoteImageUrls": "yes"}})


def test_launch_agent_no_longer_writes_logs_into_shared_tmp():
    plist = (Path(__file__).resolve().parents[1] / "Packaging"
             / "com.yukiorita.MLXBar.Coordinator.plist").read_text()
    assert "/tmp/" not in plist
    assert "StandardOutPath" not in plist
    assert "StandardErrorPath" not in plist


def test_coordinator_writes_its_log_into_the_private_root():
    from mlxbar.main import configure_logging
    import logging

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configure_logging(root)
        try:
            logging.getLogger("mlxbar.test").info("hello")
            log = root / "logs" / "coordinator.log"
            assert log.is_file()
            assert oct(log.stat().st_mode)[-3:] == "600"
        finally:
            for handler in list(logging.getLogger().handlers):
                handler.close()
                logging.getLogger().removeHandler(handler)


def test_worker_socket_permissions_are_applied_at_startup_not_shutdown():
    source = (Path(__file__).resolve().parents[1] / "Workers" / "common" / "server.py").read_text()
    startup = source.index("on_event(\"startup\")")
    chmod = source.index("os.chmod(args.socket", startup)
    run_call = source.index("uvicorn.run(app", startup)
    assert chmod < run_call, "the socket must be tightened before uvicorn starts serving"


# --------------------------------------------------------------------------
# v1.5.3: nothing is parsed before the caller is authorised
# --------------------------------------------------------------------------


def _guarded_client(**settings_patch):
    directory = tempfile.mkdtemp()
    root = Path(directory)
    settings = SettingsStore(root)
    if settings_patch:
        settings.update(settings_patch)
    state = SimpleNamespace(settings=settings, workers=WorkerSupervisor(root, settings),
                            database=Database(root / "state.sqlite3"))
    return TestClient(make_public_app(state)), settings, state


def test_request_body_is_not_parsed_before_authentication():
    """An unauthenticated caller must not be able to allocate anything.

    FastAPI resolves `body: dict` before the handler runs, so `authorize()`
    inside the handler was only reached after the whole request had been read
    and turned into Python objects.
    """
    client, settings, state = _guarded_client()
    token = settings.api_token
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi" * 1000}]}

    unauthenticated = client.post("/v1/chat/completions", json=payload)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTHENTICATION_FAILED"

    # Malformed JSON is the tell: if the body were still parsed first it would
    # come back as 422 rather than 401.
    malformed = client.post("/v1/chat/completions", content=b"{ not json",
                            headers={"Content-Type": "application/json"})
    assert malformed.status_code == 401

    authorized = client.post("/v1/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {token}"})
    assert authorized.status_code != 401
    state.database.close()


def test_health_stays_reachable_without_a_token():
    client, _settings, state = _guarded_client()
    assert client.get("/health").status_code == 200
    state.database.close()


def test_token_free_installations_are_not_blocked_by_the_guard():
    client, _settings, state = _guarded_client(api={"requireToken": False})
    assert client.post("/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 422
    state.database.close()


def test_oversized_requests_are_refused_before_the_body_is_read():
    client, settings, state = _guarded_client()
    token = settings.api_token
    limit = max_request_bytes(settings)
    response = client.post("/v1/chat/completions", content=b"{}",
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json",
                                    "Content-Length": str(limit + 1)})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "INPUT_TOO_LARGE"
    state.database.close()


def test_the_largest_legal_request_is_not_pre_rejected():
    """Eight 25 MiB images as base64 is a legal request worth ~280 MB.

    A fixed body cap would break image input outright, so the ceiling has to
    follow the same settings the handlers enforce.
    """
    with tempfile.TemporaryDirectory() as directory:
        settings = SettingsStore(Path(directory))
        generation = settings.data["generation"]
        legal = (generation["maxImages"] * generation["maxImageBytes"] * 4 // 3
                 + generation["maxPromptCharacters"] * 4)
        assert max_request_bytes(settings) > legal
        # An explicit override wins, for installations that never send images.
        settings.update({"api": {"maxRequestBytes": 16 * 1024 * 1024}})
        assert max_request_bytes(settings) == 16 * 1024 * 1024


def test_request_ceiling_survives_incomplete_settings():
    """This runs on every request; a malformed file must not become a 500."""
    assert max_request_bytes(SimpleNamespace(data={})) > 0
    assert max_request_bytes(SimpleNamespace(data={"api": {}, "generation": {}})) > 0
    assert max_request_bytes(SimpleNamespace(data={"generation": {"maxImages": "x"}})) > 0
    assert max_request_bytes(SimpleNamespace()) > 0
