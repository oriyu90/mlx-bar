"""oMLX setup service (v2.10.0).

oMLX is an external server app (https://github.com/jundot/omlx, installed via
a Homebrew tap or the standalone installer). v2.9.0 could only *connect* to a
manually installed server; this service adds the setup path used by the
Runtime settings page's oMLX card:

- detection of the `omlx` CLI and `brew` (the CLI probe is cached briefly),
- an install job (`brew install jundot/omlx/omlx`) run as a cancellable job
  with the same subprocess-group hygiene as the runtime updater,
- `omlx start` / `omlx stop` with an optional post-action reachability poll
  (the callback is injected by the caller, which owns the oMLX provider and
  therefore the configured server URL).

The service never pretends an action happened: a missing CLI/brew is a typed
error the GUI renders, and reachability is reported honestly (`serverReachable`
may be false for a server bound to a different URL). No tokens or settings are
written here — connection settings keep living in `models.omlx` (settings.py)
and `control/omlx-token`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .errors import MLXBarError

# Mirrors the runtime updater's safety valves. Homebrew downloads a
# toolchain-sized bottle the first time, so the install window is long.
INSTALL_TIMEOUT_SECONDS = 3600
SERVER_ACTION_TIMEOUT_SECONDS = 300
CLI_VERSION_TIMEOUT_SECONDS = 20
# After `omlx start`, poll the configured server URL for up to two minutes —
# enough for a first launch that pulls/initializes state — then report
# honestly that it has not answered yet instead of failing the start.
REACHABILITY_POLL_SECONDS = 120
REACHABILITY_INTERVAL_SECONDS = 3.0
PROBE_CACHE_SECONDS = 15.0

BREW_INSTALL_COMMAND = ("brew", "install", "jundot/omlx/omlx")
BREW_INSTALL_COMMAND_TEXT = "brew install jundot/omlx/omlx"
OMLX_RELEASES_URL = "https://github.com/jundot/omlx/releases"


class OMLXSetupService:
    def __init__(self):
        self._cli_cache_at = 0.0
        self._cli_cache: dict | None = None
        # One user-at-a-time: a `brew install` racing `omlx start` would probe
        # a half-installed tap. Jobs are per-action, but different actions can
        # be submitted freely, so the serialization lives here. The lock is
        # held for the whole action (including the start reachability poll),
        # so cancellations propagate normally through the waiting caller.
        self._action_lock = asyncio.Lock()

    # --- detection -----------------------------------------------------------
    @staticmethod
    def _omlx_cli_candidates() -> list[str]:
        candidates: list[str | None] = [shutil.which("omlx")]
        home_cli = Path.home() / ".omlx" / "bin" / "omlx"
        if home_cli.exists():
            candidates.append(str(home_cli))
        # Ignore non-executable leftovers from a broken install.
        return [item for item in candidates
                if item and os.path.isfile(item) and os.access(item, os.X_OK)]

    @staticmethod
    def brew_status() -> dict:
        brew = shutil.which("brew")
        if not brew or not (os.path.isfile(brew) and os.access(brew, os.X_OK)):
            fallback = "/opt/homebrew/bin/brew"
            brew = fallback if os.path.isfile(fallback) and os.access(fallback, os.X_OK) else None
        return {
            "found": bool(brew),
            "path": brew,
            "installCommand": BREW_INSTALL_COMMAND_TEXT,
        }

    @staticmethod
    def _empty_cli() -> dict:
        return {"found": False, "path": None, "version": None}

    async def probe_cli(self, force: bool = False) -> dict:
        """Detect the `omlx` CLI and its version. Cached briefly because the
        version probe spawns a process; the Runtime page calls this on every
        refresh."""
        now = time.monotonic()
        if not force and self._cli_cache and now - self._cli_cache_at < PROBE_CACHE_SECONDS:
            return dict(self._cli_cache)
        info: dict = {"found": False, "path": None, "version": None, "checkedAt": time.time()}
        values = self._omlx_cli_candidates()
        if values:
            path = values[0]
            info["found"] = True
            info["path"] = path
            try:
                output = await self._run_command([path, "--version"], None,
                                                 timeout=CLI_VERSION_TIMEOUT_SECONDS)
                for line in output.splitlines():
                    line = line.strip()
                    if line:
                        info["version"] = line
                        break
            except Exception:  # noqa: BLE001 - presence does not depend on --version
                info["version"] = None
        self._cli_cache = info
        self._cli_cache_at = now
        return dict(info)

    def requirements(self, cli: dict | None = None) -> dict:
        """Everything the Runtime page needs to render install guidance.
        `cli` is a pre-probed result; otherwise the cached probe is used."""
        if cli is None:
            cli = self._cli_cache or self._empty_cli()
        return {
            "cli": {"found": bool(cli.get("found")), "path": cli.get("path"),
                    "version": cli.get("version")},
            "brew": self.brew_status(),
            "installed": bool(cli.get("found")),
            "releasesUrl": OMLX_RELEASES_URL,
            "installCommand": BREW_INSTALL_COMMAND_TEXT,
        }

    # --- actions -------------------------------------------------------------
    async def install(self, update) -> dict:
        """Install oMLX via Homebrew as a cancellable job."""
        brew = self.brew_status().get("path")
        if not brew:
            raise MLXBarError(
                "OMLX_BREW_NOT_FOUND",
                "Homebrewが見つかりません。brewを導入するか、"
                f"手動でインストールしてください（{OMLX_RELEASES_URL}）", 409, False)
        async with self._action_lock:
            await update(0.05, f"HomebrewでoMLXをインストールしています ({BREW_INSTALL_COMMAND_TEXT})")

            async def heartbeat(seconds: int) -> None:
                await update(0.35, f"oMLXをダウンロード・インストール中（{seconds}秒経過）")

            await self._run_command([brew, *BREW_INSTALL_COMMAND[1:]], update,
                                    timeout=INSTALL_TIMEOUT_SECONDS, heartbeat=heartbeat)
            self._cli_cache_at = 0.0  # re-probe on the next refresh
            cli = await self.probe_cli(force=True)
            if not cli.get("found"):
                raise MLXBarError(
                    "OMLX_INSTALL_FAILED",
                    "Homebrewの処理は完了しましたが、omlxコマンドが見つかりません。"
                    "ターミナルで 'omlx --version' を確認してください", 409, False)
            await update(1.0, "oMLXのインストールが完了しました")
            return {"installed": True, "command": BREW_INSTALL_COMMAND_TEXT, "omlx": cli}

    async def start_server(self, update, *, verify: Callable[[], Awaitable] | None = None) -> dict:
        """`omlx start`, then poll an injected `verify()` until the configured
        server URL answers (or the poll window expires — a server bound to a
        different URL is still a successful start)."""
        cli = await self.probe_cli(force=True)
        if not cli.get("found"):
            raise MLXBarError("OMLX_NOT_INSTALLED",
                              "omlxコマンドが見つかりません。先にoMLXをインストールしてください",
                              409, False)
        async with self._action_lock:
            await update(0.2, "oMLXサーバーを起動しています (omlx start)")

            async def heartbeat(seconds: int) -> None:
                await update(0.4, f"omlxコマンドを実行中（{seconds}秒経過）")

            await self._run_command([cli["path"], "start"], update,
                                    timeout=SERVER_ACTION_TIMEOUT_SECONDS, heartbeat=heartbeat)
            reached = False
            if verify is not None:
                deadline = asyncio.get_running_loop().time() + REACHABILITY_POLL_SECONDS
                window = max(REACHABILITY_POLL_SECONDS, 1)
                attempt = 0
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        status = await verify()
                        if getattr(status, "connected", False) or (
                                isinstance(status, dict) and status.get("connected")):
                            reached = True
                            break
                    except Exception:  # noqa: BLE001 - reachability check only
                        pass
                    attempt += 1
                    await update(min(0.9, 0.5 + 0.4 * attempt * REACHABILITY_INTERVAL_SECONDS / window),
                                 "サーバーの応答を待っています")
                    await asyncio.sleep(REACHABILITY_INTERVAL_SECONDS)
            cli = await self.probe_cli(force=True)
            message = ("oMLXサーバーを起動しました" if reached
                       else "oMLXサーバーを起動しました（設定したURLにはまだ到達できません）")
            await update(1.0, message)
            return {"started": True, "serverReachable": reached, "omlx": cli,
                    "version": cli.get("version")}

    async def stop_server(self, update, *, verify: Callable[[], Awaitable] | None = None) -> dict:
        """`omlx stop`, then confirm via `verify()` that the server stopped
        answering (when available) — a refused stop is reported, not fatal."""
        cli = await self.probe_cli(force=True)
        if not cli.get("found"):
            raise MLXBarError("OMLX_NOT_INSTALLED",
                              "omlxコマンドが見つかりません。先にoMLXをインストールしてください",
                              409, False)
        async with self._action_lock:
            await update(0.3, "oMLXサーバーを停止しています (omlx stop)")

            async def heartbeat(seconds: int) -> None:
                await update(0.6, f"omlxコマンドを実行中（{seconds}秒経過）")

            await self._run_command([cli["path"], "stop"], update,
                                    timeout=SERVER_ACTION_TIMEOUT_SECONDS, heartbeat=heartbeat)
            still_reachable = None
            if verify is not None:
                try:
                    status = await verify()
                    still_reachable = bool(
                        getattr(status, "connected", False)
                        if not isinstance(status, dict) else status.get("connected"))
                except Exception:  # noqa: BLE001 - reachability check only
                    still_reachable = False
            result = {"stopped": True, "serverStillReachable": still_reachable, "omlx": cli}
            await update(1.0, "oMLXサーバーの停止コマンドを実行しました")
            return result

    # --- subprocess plumbing -------------------------------------------------
    # Same shape as RuntimeUpdater._command: one process group per command,
    # SIGTERM→SIGKILL teardown, and a per-second heartbeat callback for
    # progress rendering. Output of a failing command is preserved in the
    # error message so the GUI can show the brew/stderr tail.
    async def _run_command(self, argv: list[str], update, *, timeout: int, heartbeat=None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        communication = asyncio.create_task(proc.communicate())
        elapsed = 0
        timed_out = False
        try:
            while not communication.done():
                done, _ = await asyncio.wait({communication}, timeout=1)
                if done:
                    break
                elapsed += 1
                if elapsed >= timeout:
                    timed_out = True
                    raise asyncio.CancelledError
                if heartbeat:
                    await heartbeat(elapsed)
            out, _ = await communication
        except asyncio.CancelledError:
            await self._terminate(proc, communication)
            if timed_out:
                label = " ".join(argv[:2])
                raise MLXBarError("OMLX_COMMAND_TIMEOUT",
                                  f"oMLXのコマンド({label})が{timeout}秒以内に完了しなかったため中止しました",
                                  504, True) from None
            raise
        except BaseException:
            # v2.10.1: a failure inside the update/heartbeat plumbing (a
            # database hiccup in the job writer, for example) must not leave
            # a `brew install` running unsupervised for its remaining timeout
            # window. Tear the subprocess group down and surface the cause.
            await self._terminate(proc, communication)
            raise
        text = out.decode(errors="replace")
        if proc.returncode:
            raise MLXBarError("OMLX_COMMAND_FAILED",
                              (text[-4000:].strip() or f"oMLXのコマンド({argv[0]})に失敗しました"),
                              409, False)
        return text

    @staticmethod
    async def _terminate(proc, communication: asyncio.Task) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            await proc.wait()
        communication.cancel()
        await asyncio.gather(communication, return_exceptions=True)
