from __future__ import annotations

import hashlib
import json
import platform
import struct
from typing import Iterable

from .types import BLOCK_SIZE, FORMAT_VERSION

BLOCK_DOMAIN = b"mlxbar-paged-kv-block-v1\0"
ROOT_PARENT = "0" * 64


def token_bytes(tokens: Iterable[int]) -> bytes:
    values = []
    for token in tokens:
        value = int(token)
        if value < 0 or value > 0xFFFFFFFF:
            raise ValueError("token id is outside uint32")
        values.append(value)
    return struct.pack(f"<{len(values)}I", *values)


def token_digest(tokens: Iterable[int]) -> str:
    return hashlib.sha256(token_bytes(tokens)).hexdigest()


def chained_hash(parent_hash: str, tokens: Iterable[int], semantic_salt: str) -> str:
    if len(parent_hash) != 64:
        raise ValueError("invalid parent hash")
    digest = hashlib.sha256()
    digest.update(BLOCK_DOMAIN)
    digest.update(bytes.fromhex(parent_hash))
    digest.update(token_bytes(tokens))
    digest.update(b"\0")
    digest.update(semantic_salt.encode("utf-8", errors="surrogateescape"))
    return digest.hexdigest()


def chain(tokens: list[int], semantic_salt: str) -> list[dict]:
    parent = ROOT_PARENT
    result = []
    for start in range(0, len(tokens) - BLOCK_SIZE + 1, BLOCK_SIZE):
        block = tokens[start:start + BLOCK_SIZE]
        block_hash = chained_hash(parent, block, semantic_salt)
        result.append({"hash": block_hash, "parent": parent, "start": start,
                       "tokens": token_digest(block)})
        parent = block_hash
    return result


def namespace_fingerprint(model_fingerprint: str, runtime_version: str,
                          mlx_version: str, layer_types: list[str]) -> str:
    payload = {
        "format": FORMAT_VERSION,
        "blockSize": BLOCK_SIZE,
        "model": model_fingerprint,
        "runtime": runtime_version,
        "mlx": mlx_version,
        "python": platform.python_version(),
        "layers": layer_types,
        "quantization": "none",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"mlxbar-paged-kv-namespace-v1\0" + encoded).hexdigest()
