from __future__ import annotations

import json
import os
import secrets
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "schemaVersion": 1,
    "api": {"enabled": True, "host": "127.0.0.1", "port": 11435, "requireToken": True,
            # 0 derives the ceiling from the generation limits below.
            "maxRequestBytes": 0, "maxConcurrentConnections": 64},
    "models": {
        "watchFolders": True,
        "autoLoadOnAPIRequest": True,
        "roots": [],
        "pool": {
            # Keep independent model processes warm while preserving the
            # pre-v1.6.2 global single-generation contract.  A process boundary
            # contains runtime/model crashes and gives each model an allocator
            # ceiling of its own.
            "enabled": True,
            "maxResidentModels": 2,
            "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 4,
            "defaultPerModelMaxGB": 32,
            "idleTTLSeconds": 900,
            # Loads are deliberately serial: two simultaneous cold loads have
            # the least predictable combined allocation peak.
            "loadConcurrency": 1,
            "profiles": [],
        },
        "lmStudio": {
            "enabled": True,
            "folder": None,
            "baseUrl": "http://127.0.0.1:1234",
            "autoLoad": True,
        },
    },
    "runtimes": {
        # Off by default: a runtime that changes underneath a working large
        # model invalidates the persistent prompt cache and can break a setup
        # that was fine a moment earlier. Updating stays an explicit action.
        "mlx-lm": {"channel": "stable", "autoCheck": False},
        "mlx-vlm": {"channel": "stable", "autoCheck": False},
        "autoInstallMissing": True,
        "checkIntervalHours": 168,
        "keepSlots": 3,
    },
    "generation": {
        "maxTokens": 8192,
        "defaultTemperature": 0.7,
        "defaultTopP": 1.0,
        "defaultRepetitionPenalty": 1.0,
        "repetitionContextSize": 20,
        "maxPromptCharacters": 100000,
        "maxImages": 8,
        "maxImageBytes": 26214400,
        "loadTimeoutSeconds": 600,
        "tokenIdleTimeoutSeconds": 60,
        "streamHeartbeatSeconds": 10,
        "maxQueuedRequests": 16,
        "queueTimeoutSeconds": 3600,
        "totalTimeoutSeconds": 3600,
        "cancelGraceSeconds": 5,
        "memoryLimitRatio": 0.90,
        "wiredLimitRatio": 0.80,
        "cacheLimitRatio": 0.10,
    },
    "promptCache": {
        "diskEnabled": True,
        "diskMaxGB": 10,
        "keepGenerations": 2,
        "memoryRatio": 0.10,
        # Snapshots of a completed turn, for architectures whose cache cannot be
        # rolled back in place. "auto" enables them only where they are the only
        # way to reuse anything after a branch; "off" keeps the pre-1.6.0
        # behaviour of a full prefill in that case.
        "branchCheckpoint": "auto",
        # Ceiling on snapshot writes per worker lifetime. One snapshot of a long
        # conversation is measured in gigabytes, so an unbounded disk tier is a
        # sustained write load rather than a cache.
        "diskWriteBudgetGB": 32,
        # APC's own in-memory block pool. Left off because its behaviour on a
        # 27B-class hybrid has not been measured; see mlx-bar.md.
        "memoryBlocks": "off",
    },
    "security": {"trustRemoteCodeDefault": False, "allowLan": False,
                 "allowRemoteImageUrls": False},
    "general": {"continueAfterGUIExit": True, "launchAtLogin": False, "logLevel": "info",
                "language": "en", "preloadLastModel": True},
}


def app_support_dir() -> Path:
    override = os.environ.get("MLXBAR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "MLXBar"


def deep_merge(defaults: dict, stored: dict) -> dict:
    result = deepcopy(defaults)
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class SettingsStore:
    def __init__(self, root: Path | None = None):
        self.root = root or app_support_dir()
        self.path = self.root / "config.json"
        self.token_path = self.root / "control" / "api-token"
        self.lm_studio_token_path = self.root / "control" / "lmstudio-token"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "control").mkdir(mode=0o700, exist_ok=True)
        (self.root / "logs").mkdir(exist_ok=True)
        self.recovered_from: str | None = None
        self.data = self._load()
        self._ensure_token()

    def _load(self) -> dict:
        if not self.path.exists():
            data = deepcopy(DEFAULTS)
            self._atomic_write(data)
            return data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schemaVersion", 1) > 1:
                raise ValueError("unsupported settings schema")
            data = deep_merge(DEFAULTS, raw)
            self._validate(data)
            return data
        except Exception:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.root / f"config.invalid-{stamp}.json"
            shutil.copy2(self.path, backup)
            # Every setting silently returns to its default here -- LAN access,
            # port, token limits. Record it so status can say so instead of
            # leaving the user to discover it through changed behaviour.
            self.recovered_from = backup.name
            data = deepcopy(DEFAULTS)
            self._atomic_write(data)
            return data

    def _atomic_write(self, data: dict) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    @staticmethod
    def _validate(data: dict) -> None:
        api = data["api"]
        host = api.get("host")
        allow_lan = data.get("security", {}).get("allowLan", False)
        if host not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("api.host must be 127.0.0.1 or 0.0.0.0")
        if allow_lan != (host == "0.0.0.0"):
            raise ValueError("api.host and security.allowLan must be changed together")
        prompt_cache = data.get("promptCache", {})
        if not isinstance(prompt_cache.get("diskEnabled", True), bool):
            raise ValueError("promptCache.diskEnabled must be boolean")
        disk_max_gb = prompt_cache.get("diskMaxGB", 10)
        if (isinstance(disk_max_gb, bool) or not isinstance(disk_max_gb, (int, float))
                or not 1 <= float(disk_max_gb) <= 100):
            raise ValueError("promptCache.diskMaxGB must be between 1 and 100")
        memory_ratio = prompt_cache.get("memoryRatio", 0.10)
        if (isinstance(memory_ratio, bool) or not isinstance(memory_ratio, (int, float))
                or not 0 <= float(memory_ratio) <= 0.5):
            raise ValueError("promptCache.memoryRatio must be between 0 and 0.5")
        if prompt_cache.get("branchCheckpoint", "auto") not in {"auto", "off"}:
            raise ValueError("promptCache.branchCheckpoint must be auto or off")
        if prompt_cache.get("memoryBlocks", "off") not in {"auto", "off"}:
            raise ValueError("promptCache.memoryBlocks must be auto or off")
        write_budget = prompt_cache.get("diskWriteBudgetGB", 32)
        if (not isinstance(write_budget, (int, float)) or isinstance(write_budget, bool)
                or not 0 <= float(write_budget) <= 4096):
            raise ValueError("promptCache.diskWriteBudgetGB must be between 0 and 4096")
        keep_generations = prompt_cache.get("keepGenerations", 2)
        if (isinstance(keep_generations, bool) or not isinstance(keep_generations, int)
                or not 1 <= keep_generations <= 10):
            raise ValueError("promptCache.keepGenerations must be between 1 and 10")
        if allow_lan and not api.get("requireToken", True):
            raise ValueError("LAN公開中はAPIキーを無効にできません")
        if not isinstance(data.get("security", {}).get("allowRemoteImageUrls", False), bool):
            raise ValueError("security.allowRemoteImageUrls must be boolean")
        port = api.get("port")
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        max_request_bytes = api.get("maxRequestBytes", 0)
        if (isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int)
                or not 0 <= max_request_bytes <= 4_294_967_296):
            raise ValueError("api.maxRequestBytes must be between 0 and 4294967296")
        connections = api.get("maxConcurrentConnections", 64)
        if (isinstance(connections, bool) or not isinstance(connections, int)
                or not 1 <= connections <= 1024):
            raise ValueError("api.maxConcurrentConnections must be between 1 and 1024")
        if not isinstance(data.get("models", {}).get("autoLoadOnAPIRequest"), bool):
            raise ValueError("models.autoLoadOnAPIRequest must be boolean")
        pool = data.get("models", {}).get("pool", {})
        if not isinstance(pool.get("enabled", True), bool):
            raise ValueError("models.pool.enabled must be boolean")
        pool_integer_ranges = {
            "maxResidentModels": (1, 8),
            "idleTTLSeconds": (30, 86400),
            "loadConcurrency": (1, 1),
        }
        for key, (minimum, maximum) in pool_integer_ranges.items():
            value = pool.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"models.pool.{key} must be between {minimum} and {maximum}")
        total_ratio = pool.get("totalMemoryRatio")
        if (isinstance(total_ratio, bool) or not isinstance(total_ratio, (int, float))
                or not 0.5 <= float(total_ratio) <= 0.9):
            raise ValueError("models.pool.totalMemoryRatio must be between 0.5 and 0.9")
        for key, minimum, maximum in (("minimumSystemReserveGB", 1, 128),
                                      ("defaultPerModelMaxGB", 1, 512)):
            value = pool.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not minimum <= float(value) <= maximum):
                raise ValueError(f"models.pool.{key} must be between {minimum} and {maximum}")
        profiles = pool.get("profiles")
        if not isinstance(profiles, list) or len(profiles) > 64:
            raise ValueError("models.pool.profiles must be an array with at most 64 entries")
        seen_profiles: set[str] = set()
        for profile in profiles:
            if not isinstance(profile, dict) or not isinstance(profile.get("modelId"), str):
                raise ValueError("each models.pool profile needs a modelId")
            model_id = profile["modelId"].strip()
            if not model_id or model_id in seen_profiles:
                raise ValueError("models.pool profile modelId values must be unique and non-empty")
            seen_profiles.add(model_id)
            if not isinstance(profile.get("keepLoaded", False), bool):
                raise ValueError("models.pool profile keepLoaded must be boolean")
            maximum = profile.get("maxMemoryGB", pool.get("defaultPerModelMaxGB"))
            if (isinstance(maximum, bool) or not isinstance(maximum, (int, float))
                    or not 1 <= float(maximum) <= 512):
                raise ValueError("models.pool profile maxMemoryGB must be between 1 and 512")
        if data.get("general", {}).get("language") not in {"en", "ja"}:
            raise ValueError("general.language must be en or ja")
        if not isinstance(data.get("general", {}).get("preloadLastModel", True), bool):
            raise ValueError("general.preloadLastModel must be boolean")
        generation = data.get("generation", {})
        integer_ranges = {
            "maxTokens": (1, 2_000_000),
            "repetitionContextSize": (1, 32768),
            "maxPromptCharacters": (1, 10_000_000),
            "maxImages": (0, 128),
            "maxImageBytes": (1, 2_147_483_648),
            "loadTimeoutSeconds": (10, 3600),
            "tokenIdleTimeoutSeconds": (5, 600),
            "streamHeartbeatSeconds": (1, 30),
            "maxQueuedRequests": (1, 64),
            "queueTimeoutSeconds": (10, 7200),
            "totalTimeoutSeconds": (10, 7200),
            "cancelGraceSeconds": (1, 30),
        }
        for key, (minimum, maximum) in integer_ranges.items():
            value = generation.get(key)
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"generation.{key} must be between {minimum} and {maximum}")
        numeric_ranges = {
            "defaultTemperature": (0.0, 2.0),
            "defaultTopP": (0.0, 1.0),
            "defaultRepetitionPenalty": (0.01, 2.0),
        }
        for key, (minimum, maximum) in numeric_ranges.items():
            value = generation.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"generation.{key} must be between {minimum} and {maximum}")
        memory_ratio = generation.get("memoryLimitRatio")
        if not isinstance(memory_ratio, (int, float)) or not 0.5 <= memory_ratio <= 0.99:
            raise ValueError("generation.memoryLimitRatio must be between 0.5 and 0.99")
        # 0 disables the corresponding MLX limit and keeps the runtime default.
        for key, maximum in (("wiredLimitRatio", 0.95), ("cacheLimitRatio", 0.5)):
            value = generation.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= float(value) <= maximum):
                raise ValueError(f"generation.{key} must be between 0 and {maximum}")
        if float(generation.get("wiredLimitRatio", 0)) > float(memory_ratio):
            raise ValueError("generation.wiredLimitRatio must not exceed generation.memoryLimitRatio")

    def update(self, patch: dict) -> dict:
        merged = deep_merge(self.data, patch)
        self._validate(merged)
        self._atomic_write(merged)
        self.data = merged
        return self.public()

    def public(self) -> dict:
        return deepcopy(self.data)

    def _ensure_token(self) -> None:
        if not self.token_path.exists():
            self._write_secret(self.token_path, secrets.token_urlsafe(32))

    @staticmethod
    def _write_secret(path: Path, value: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @property
    def api_token(self) -> str:
        """Cached token, re-read only when the file actually changes.

        `authorize()` runs on every API request, including each one that then
        streams tokens, so this used to be a disk read in the event loop on the
        hot path.
        """
        try:
            stamp = self.token_path.stat().st_mtime_ns
        except OSError:
            self._token_cache = None
            raise
        cached = getattr(self, "_token_cache", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        value = self.token_path.read_text(encoding="utf-8").strip()
        self._token_cache = (stamp, value)
        return value

    def regenerate_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._write_secret(self.token_path, token)
        return token

    def set_api_token(self, token: str) -> str:
        if not isinstance(token, str):
            raise ValueError("APIキーは文字列で指定してください")
        token = token.strip()
        if not 16 <= len(token) <= 512 or any(character.isspace() for character in token):
            raise ValueError("APIキーは空白を含まない16〜512文字で指定してください")
        self._write_secret(self.token_path, token)
        return token

    @property
    def lm_studio_token(self) -> str | None:
        if not self.lm_studio_token_path.exists():
            return None
        token = self.lm_studio_token_path.read_text(encoding="utf-8").strip()
        return token or None

    def set_lm_studio_token(self, token: str | None) -> str | None:
        if token is not None and not isinstance(token, str):
            raise ValueError("LM Studio APIキーは文字列で指定してください")
        token = (token or "").strip()
        if not token:
            self.lm_studio_token_path.unlink(missing_ok=True)
            return None
        if len(token) > 2048 or any(character in "\r\n" for character in token):
            raise ValueError("LM Studio APIキーが不正です")
        self._write_secret(self.lm_studio_token_path, token)
        return token
