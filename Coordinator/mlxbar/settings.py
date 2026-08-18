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
    "api": {"enabled": True, "host": "127.0.0.1", "port": 11435, "requireToken": True},
    "models": {
        "watchFolders": True,
        "autoLoadOnAPIRequest": True,
        "roots": [],
        "lmStudio": {
            "enabled": True,
            "folder": None,
            "baseUrl": "http://127.0.0.1:1234",
            "autoLoad": True,
        },
    },
    "runtimes": {
        "mlx-lm": {"channel": "stable", "autoCheck": True},
        "mlx-vlm": {"channel": "stable", "autoCheck": True},
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
        "totalTimeoutSeconds": 900,
        "cancelGraceSeconds": 5,
        "memoryLimitRatio": 0.90,
    },
    "security": {"trustRemoteCodeDefault": False, "allowLan": False,
                 "allowRemoteImageUrls": False},
    "general": {"continueAfterGUIExit": True, "launchAtLogin": False, "logLevel": "info", "language": "en"},
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
            shutil.copy2(self.path, self.root / f"config.invalid-{stamp}.json")
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
        if allow_lan and not api.get("requireToken", True):
            raise ValueError("LAN公開中はAPIキーを無効にできません")
        if not isinstance(data.get("security", {}).get("allowRemoteImageUrls", False), bool):
            raise ValueError("security.allowRemoteImageUrls must be boolean")
        port = api.get("port")
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        if not isinstance(data.get("models", {}).get("autoLoadOnAPIRequest"), bool):
            raise ValueError("models.autoLoadOnAPIRequest must be boolean")
        if data.get("general", {}).get("language") not in {"en", "ja"}:
            raise ValueError("general.language must be en or ja")
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
        return self.token_path.read_text(encoding="utf-8").strip()

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
