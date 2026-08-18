"""Resolve `image_url` values that arrive over the public OpenAI-compatible API.

The public listener can be exposed to the LAN, so an image reference from that
surface is untrusted input. Passing it straight to the vision runtime would let
a caller name any readable path on this Mac, or any URL the Mac can reach, and
read back its content through the model's description. Every reference is
therefore rewritten into a private file inside a per-request directory before it
reaches a worker, and anything that cannot be rewritten safely is rejected.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..errors import MLXBarError


DATA_PREFIX = "data:"
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
REMOTE_FETCH_TIMEOUT_SECONDS = 20


class ImageWorkspace:
    """Private directory holding the files handed to a worker for one request."""

    def __init__(self, root: Path):
        self.root = root

    @property
    def path(self) -> Path:
        return self.root

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _reject(message: str) -> MLXBarError:
    return MLXBarError("INVALID_REQUEST", message, 422)


def _decode_data_uri(value: str, limit: int) -> tuple[bytes, str]:
    header, _, payload = value[len(DATA_PREFIX):].partition(",")
    if not payload:
        raise _reject("image_urlのdata URIが不正です")
    parameters = header.split(";")
    media_type = (parameters[0] or "image/png").strip().lower()
    if media_type not in ALLOWED_IMAGE_TYPES:
        raise _reject(f"対応していない画像形式です: {media_type or 'unknown'}")
    if "base64" not in [item.strip().lower() for item in parameters[1:]]:
        raise _reject("image_urlのdata URIはbase64のみ対応しています")
    # 4 base64 characters encode 3 bytes; check before decoding so an oversized
    # payload is refused without materialising it.
    if len(payload) // 4 * 3 > limit:
        raise MLXBarError("INPUT_TOO_LARGE",
                          f"画像は1件{limit // 1_048_576}MB以内にしてください", 413)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _reject("image_urlのbase64データを復号できません") from exc
    if len(data) > limit:
        raise MLXBarError("INPUT_TOO_LARGE",
                          f"画像は1件{limit // 1_048_576}MB以内にしてください", 413)
    return data, ALLOWED_IMAGE_TYPES[media_type]


def _assert_public_host(host: str) -> None:
    """Refuse hosts that resolve into the Mac itself or its private networks."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise _reject(f"画像URLのホストを解決できません: {host}") from exc
    if not infos:
        raise _reject(f"画像URLのホストを解決できません: {host}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise _reject("プライベートアドレスの画像URLは取得できません")


async def _fetch_remote(url: str, limit: int) -> tuple[bytes, str]:
    parts = urlsplit(url)
    if not parts.hostname:
        raise _reject("画像URLが不正です")
    _assert_public_host(parts.hostname)
    try:
        # Redirects are disabled so a permitted host cannot bounce the request
        # onto a private address that `_assert_public_host` already refused.
        async with httpx.AsyncClient(timeout=REMOTE_FETCH_TIMEOUT_SECONDS,
                                     follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise _reject(f"画像URLを取得できません（HTTP {response.status_code}）")
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type not in ALLOWED_IMAGE_TYPES:
                    raise _reject(f"対応していない画像形式です: {media_type or 'unknown'}")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise MLXBarError("INPUT_TOO_LARGE",
                                          f"画像は1件{limit // 1_048_576}MB以内にしてください", 413)
    except MLXBarError:
        raise
    except httpx.HTTPError as exc:
        raise _reject(f"画像URLを取得できません: {exc}") from exc
    return bytes(data), ALLOWED_IMAGE_TYPES[media_type]


async def resolve_public_images(urls: list[str], settings) -> tuple[list[str], ImageWorkspace | None]:
    """Rewrite untrusted image references into files under a private directory.

    Returns the worker-facing paths plus the workspace that owns them; the
    caller must call `cleanup()` once generation has finished.
    """
    if not urls:
        return [], None
    generation = settings.data["generation"]
    limit = int(generation["maxImageBytes"])
    maximum = int(generation["maxImages"])
    if len(urls) > maximum:
        raise MLXBarError("INPUT_TOO_LARGE", f"画像は最大{maximum}件です", 413)
    allow_remote = bool(settings.data.get("security", {}).get("allowRemoteImageUrls", False))
    workspace = ImageWorkspace(Path(tempfile.mkdtemp(prefix="mlxbar-images-")))
    workspace.path.chmod(0o700)
    paths: list[str] = []
    try:
        for index, url in enumerate(urls):
            value = url.strip()
            if value.startswith(DATA_PREFIX):
                data, suffix = _decode_data_uri(value, limit)
            elif value.lower().startswith(("http://", "https://")):
                if not allow_remote:
                    raise _reject(
                        "http(s)の画像URLは既定で無効です。data URI（base64）で送信するか、"
                        "設定で外部画像URLの取得を有効にしてください")
                data, suffix = await _fetch_remote(value, limit)
            else:
                # Bare filesystem paths and file:// URLs would let a caller read
                # any image this Mac can open, so they never cross the public API.
                raise _reject("image_urlはdata URI（base64）で指定してください")
            target = workspace.path / f"image-{index}{suffix}"
            target.write_bytes(data)
            target.chmod(0o600)
            paths.append(str(target))
    except BaseException:
        workspace.cleanup()
        raise
    return paths, workspace
