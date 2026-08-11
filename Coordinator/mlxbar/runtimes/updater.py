from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import sys
import time
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from ..errors import MLXBarError


VERSION = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z]+){1,3}$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")


class RuntimeUpdater:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()

    async def check(self, engine: str) -> dict:
        if engine not in {"mlx-lm", "mlx-vlm"}:
            raise MLXBarError("INVALID_ENGINE", "不明なランタイムです", 400)
        base = os.environ.get("MLXBAR_PYPI_BASE_URL", "https://pypi.org/pypi").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                         headers={"Cache-Control": "no-cache", "Pragma": "no-cache"}) as client:
                response = await client.get(f"{base}/{engine}/json")
                response.raise_for_status()
                payload = response.json()
                info = payload["info"]
        except Exception as exc:
            raise MLXBarError("UPDATE_CHECK_FAILED", f"最新版を確認できません: {exc}", 503, True) from exc

        active = self.store.active(engine).get("active")
        current = None
        if active:
            probe = self.store.engine_root(engine) / "slots" / active / "probe.json"
            if probe.exists():
                current = json.loads(probe.read_text()).get("version")
        candidate = info.get("version")
        if not isinstance(candidate, str) or not candidate:
            raise MLXBarError("UPDATE_CHECK_FAILED", "最新版のバージョン情報がありません", 503, True)
        try:
            candidate_parsed = Version(candidate)
            current_parsed = Version(current) if current else None
        except InvalidVersion as exc:
            raise MLXBarError("UPDATE_CHECK_FAILED", f"バージョン情報を判定できません: {exc}", 503, True) from exc
        update_available = current_parsed is None or current_parsed < candidate_parsed
        if current_parsed is None:
            version_status = "not_installed"
        elif current_parsed == candidate_parsed:
            version_status = "latest"
        elif current_parsed > candidate_parsed:
            version_status = "newer_than_stable"
        else:
            version_status = "update_available"
        return {
            "engine": engine,
            "channel": "stable",
            "currentVersion": current,
            "candidateVersion": candidate,
            "updateAvailable": update_available,
            "versionStatus": version_status,
            "requiresPython": info.get("requires_python"),
            "releaseUrl": info.get("project_url") or info.get("package_url"),
            "source": "pypi",
        }

    async def stage(self, engine: str, update, version: str | None = None, git_ref: str | None = None) -> dict:
        package = engine
        if version and not VERSION.fullmatch(version):
            raise MLXBarError("INVALID_VERSION", "バージョン形式が不正です", 422)
        if git_ref and not SHA.fullmatch(git_ref):
            raise MLXBarError("INVALID_GIT_REF", "git-refはcommit SHAで指定してください", 422)
        async with self.lock:
            usage = shutil.disk_usage(self.store.engine_root(engine))
            if usage.free < 1_000_000_000:
                raise MLXBarError("INSUFFICIENT_STORAGE", "ランタイム更新には1GB以上の空き容量が必要です", 507)
            slot_id = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000:03d}"
            slot = self.store.engine_root(engine) / "slots" / slot_id
            slot.mkdir(parents=True)
            try:
                await update(0.08, "新しいPython環境を作成中")
                uv = self._uv_executable()
                await self._command(uv, "venv", "--python", "3.12", str(slot / ".venv"))
                spec = package
                source = "stable"
                if version:
                    spec = f"{package}=={version}"
                    source = version
                elif git_ref:
                    repo = "https://github.com/ml-explore/mlx-lm.git" if engine == "mlx-lm" else "https://github.com/Blaizzy/mlx-vlm.git"
                    spec = f"git+{repo}@{git_ref}"
                    source = git_ref
                await update(0.28, f"{package}をダウンロード・インストール中")
                python = slot / ".venv" / "bin" / "python"
                async def download_heartbeat(elapsed: int) -> None:
                    await update(0.28, f"{package}をダウンロード・インストール中（{elapsed}秒経過）")

                await self._command(uv, "pip", "install", "--python", str(python), spec,
                                    "fastapi>=0.115,<1", "uvicorn>=0.30,<1",
                                    heartbeat=download_heartbeat)
                await update(0.65, "依存関係を検証中")
                await self._command(uv, "pip", "check", "--python", str(python))
                frozen = await self._command(uv, "pip", "freeze", "--python", str(python))
                (slot / "requirements.lock").write_text(frozen)
                await update(0.78, "アダプター互換性を検証中")
                module = "mlx_lm" if engine == "mlx-lm" else "mlx_vlm"
                code = (
                    "import importlib,json,sys; from importlib.metadata import version; m=importlib.import_module(sys.argv[1]); "
                    "assert callable(getattr(m,'load',None)); assert callable(getattr(m,'stream_generate',None)); "
                    "print(json.dumps({'compatible':True,'python':sys.version.split()[0],"
                    "'version':version(sys.argv[2]),'streaming':True,'localPath':True,'contractVersion':1}))"
                )
                stdout = await self._command(str(python), "-c", code, module, package)
                probe = json.loads(stdout)
                (slot / "probe.json").write_text(json.dumps(probe, indent=2) + "\n")
                (slot / "manifest.json").write_text(json.dumps({
                    "engine": engine, "source": source, "package": spec,
                    "resolvedVersion": probe.get("version"), "createdAt": time.time(),
                }, indent=2) + "\n")
                await update(0.9, "新しいslotの検証が完了")
                return {"slotId": slot_id, "probe": probe, "manifest": {"package": spec, "source": source}}
            except BaseException:
                shutil.rmtree(slot, ignore_errors=True)
                raise

    async def _command(self, *arguments: str, heartbeat=None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        communication = asyncio.create_task(proc.communicate())
        elapsed = 0
        try:
            while not communication.done():
                done, _ = await asyncio.wait({communication}, timeout=1)
                if done:
                    break
                elapsed += 1
                if heartbeat:
                    await heartbeat(elapsed)
            out, _ = await communication
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
            raise
        text = out.decode(errors="replace")
        if proc.returncode:
            raise MLXBarError("UPDATE_PROBE_FAILED", text[-4000:] or "更新コマンドに失敗しました", 409)
        return text

    @staticmethod
    def _uv_executable() -> str:
        override = os.environ.get("MLXBAR_UV_EXECUTABLE")
        if override:
            return override
        executable = Path(sys.executable).resolve()
        candidates = [
            executable.parents[1] / "MLXBar_MLXBar.bundle" / "uv",
            executable.parents[1] / "Resources" / "MLXBar_MLXBar.bundle" / "uv",
        ]
        return str(next((item for item in candidates if item.exists()), "uv"))
