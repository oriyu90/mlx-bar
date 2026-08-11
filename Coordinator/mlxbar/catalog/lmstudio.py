from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


async def cli_models() -> list[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "lms", "ls", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return []
        payload = json.loads(stdout)
    except Exception:
        return []
    items = payload if isinstance(payload, list) else payload.get("models", [])
    result = []
    for item in items:
        path = item.get("path") or item.get("location")
        key = item.get("key") or item.get("modelKey") or item.get("id")
        if key:
            result.append({"provider_key": str(key), "path": str(Path(path).expanduser()) if path else None,
                           "compatibility_type": item.get("compatibility_type") or item.get("format")})
    return result


async def api_models(base_url: str, token: str | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(base_url.rstrip("/") + "/v1/models", headers=headers)
            response.raise_for_status()
            data = response.json().get("data", [])
            return [{"provider_key": str(item["id"]), "path": None, "compatibility_type": None}
                    for item in data if item.get("id")]
    except Exception:
        return []
