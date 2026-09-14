from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .types import CorruptBlock


class DiskBlockStore:
    def __init__(self, root: Path, namespace: str, max_bytes: int):
        self.root = root
        self.namespace = namespace
        self.max_bytes = max(0, int(max_bytes))
        self.directory = root / f"mlxbar-paged-v1-{namespace}"
        self.quarantine = self.directory / "quarantine"
        self._secure_directory(root, parents=True)
        self._secure_directory(self.directory)
        self._secure_directory(self.quarantine)
        self._remove_temporaries()
        self.prune()

    def path(self, block_hash: str) -> Path:
        if len(block_hash) != 64 or any(c not in "0123456789abcdef" for c in block_hash):
            raise ValueError("invalid block hash")
        return self.directory / f"{block_hash}.safetensors"

    def exists(self, block_hash: str) -> bool:
        try:
            path = self.path(block_hash)
            checksum = self._checksum_path(path)
            return (path.is_file() and not path.is_symlink() and checksum.is_file()
                    and not checksum.is_symlink())
        except (OSError, ValueError):
            return False

    def save(self, block_hash: str, arrays: dict, metadata: dict) -> bool:
        final = self.path(block_hash)
        final_checksum = self._checksum_path(final)
        if self._complete_regular_pair(final, final_checksum):
            return False
        # A crash can happen between the tensor and checksum renames. Such a
        # half-pair is never readable; remove it here so the running worker can
        # heal on the next store instead of waiting for a process restart.
        self._remove_incomplete_pair(final, final_checksum)
        temporary = self.directory / f".{block_hash}.{uuid.uuid4().hex}.tmp.safetensors"
        temporary_checksum = self._checksum_path(temporary)
        try:
            import mlx.core as mx
            text_meta = {str(key): (value if isinstance(value, str) else json.dumps(
                value, sort_keys=True, separators=(",", ":"))) for key, value in metadata.items()}
            mx.save_safetensors(str(temporary), arrays, metadata=text_meta)
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            checksum = self._file_digest(temporary)
            temporary_checksum.write_text(checksum + "\n", encoding="ascii")
            os.chmod(temporary_checksum, 0o600)
            with temporary_checksum.open("rb") as handle:
                os.fsync(handle.fileno())
            if self._complete_regular_pair(final, final_checksum):
                temporary.unlink(missing_ok=True)
                temporary_checksum.unlink(missing_ok=True)
                return False
            self._remove_incomplete_pair(final, final_checksum)
            os.replace(temporary, final)
            os.replace(temporary_checksum, final_checksum)
            self._fsync_directory(self.directory)
            return True
        except Exception:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                temporary_checksum.unlink(missing_ok=True)
            raise

    def load(self, block_hash: str, *, max_file_bytes: int | None = None) -> tuple[dict, dict]:
        path = self.path(block_hash)
        if path.is_symlink() or not path.is_file():
            raise CorruptBlock("block_missing")
        try:
            if max_file_bytes is not None and path.stat().st_size > max(0, int(max_file_bytes)):
                raise CorruptBlock("block_file_too_large")
            checksum_path = self._checksum_path(path)
            if checksum_path.is_symlink() or not checksum_path.is_file():
                raise CorruptBlock("checksum_missing")
            expected = checksum_path.read_text(encoding="ascii").strip()
            if len(expected) != 64 or self._file_digest(path) != expected:
                raise CorruptBlock("checksum_mismatch")
            import mlx.core as mx
            arrays, metadata = mx.load(str(path), return_metadata=True)
            with contextlib.suppress(OSError):
                os.utime(path)
            return dict(arrays), dict(metadata)
        except CorruptBlock:
            raise
        except Exception as exc:
            raise CorruptBlock(f"block_unreadable:{type(exc).__name__}") from exc

    def mark_exact(self, prompt_digest: str, terminal_hash: str,
                   prompt_length: int, blocks: int) -> None:
        if len(prompt_digest) != 64:
            raise ValueError("invalid prompt digest")
        directory = self.directory / "exact"
        self._secure_directory(directory)
        final = directory / f"{prompt_digest}.exact.json"
        temporary = directory / f".{prompt_digest}.{uuid.uuid4().hex}.tmp"
        payload = {"namespace": self.namespace, "terminal": terminal_hash,
                   "promptLength": int(prompt_length), "blocks": int(blocks)}
        try:
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            self._fsync_directory(directory)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        self.prune()

    def exact_matches(self, prompt_digest: str, terminal_hash: str,
                      prompt_length: int, blocks: int) -> bool:
        try:
            path = self.directory / "exact" / f"{prompt_digest}.exact.json"
            if path.is_symlink() or not path.is_file():
                return False
            payload = json.loads(path.read_text(encoding="utf-8"))
            expected = {"namespace": self.namespace, "terminal": terminal_hash,
                        "promptLength": int(prompt_length), "blocks": int(blocks)}
            if payload != expected:
                path.unlink(missing_ok=True)
                return False
            os.utime(path)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def quarantine_block(self, block_hash: str, reason: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            source = self.path(block_hash)
            safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:48]
            target = self.quarantine / f"{block_hash}.{safe_reason}.{int(time.time())}.safetensors"
            if source.exists() and not source.is_symlink():
                source.replace(target)
            checksum = self._checksum_path(source)
            if checksum.exists() and not checksum.is_symlink():
                checksum.replace(self._checksum_path(target))
            self.prune()

    def prune(self) -> None:
        if self.max_bytes <= 0:
            return
        files = []
        with contextlib.suppress(OSError):
            candidates = [*self.root.rglob("*.safetensors"),
                          *self.root.rglob("*.exact.json")]
            for path in candidates:
                if path.is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    stat = path.stat()
                    checksum_size = 0
                    if path.suffix == ".safetensors":
                        with contextlib.suppress(OSError):
                            checksum_size = self._checksum_path(path).stat().st_size
                    # Exact-only markers are expendable indexes, so evict them
                    # before data blocks. They must never crowd out the prefix
                    # data needed when branch reuse is enabled.
                    priority = 0 if path.name.endswith(".exact.json") else 1
                    files.append((priority, stat.st_mtime, stat.st_size + checksum_size, path))
        total = sum(item[2] for item in files)
        for _, _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                path.unlink()
                if path.suffix == ".safetensors":
                    with contextlib.suppress(OSError):
                        self._checksum_path(path).unlink()
                total -= size
            except OSError:
                continue

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secure_directory(self.root, parents=True)
        self._secure_directory(self.directory)
        self.quarantine = self.directory / "quarantine"
        self._secure_directory(self.quarantine)

    def stats(self) -> tuple[int, int]:
        count = 0
        with contextlib.suppress(OSError):
            for path in self.directory.glob("*.safetensors"):
                if path.is_file() and not path.is_symlink():
                    count += 1
        total = 0
        with contextlib.suppress(OSError):
            for path in self.root.rglob("*"):
                if path.is_file() and not path.is_symlink() and ".tmp" not in path.name:
                    with contextlib.suppress(OSError):
                        total += path.stat().st_size
        return count, total

    def _remove_temporaries(self) -> None:
        with contextlib.suppress(OSError):
            for path in self.directory.glob(".*.tmp.safetensors"):
                with contextlib.suppress(OSError):
                    path.unlink()
                with contextlib.suppress(OSError):
                    self._checksum_path(path).unlink()
            for path in self.directory.glob(".*.tmp.safetensors.sha256"):
                with contextlib.suppress(OSError):
                    path.unlink()
            # A crash between the two atomic renames leaves an unverifiable
            # tensor. It is never a cache hit and is reclaimed at startup.
            for path in self.directory.glob("*.safetensors"):
                if not self._checksum_path(path).is_file():
                    with contextlib.suppress(OSError):
                        path.unlink()
            for path in self.directory.glob("*.safetensors.sha256"):
                tensor = path.with_name(path.name.removesuffix(".sha256"))
                if not tensor.is_file() or tensor.is_symlink():
                    with contextlib.suppress(OSError):
                        path.unlink()

    @staticmethod
    def _complete_regular_pair(path: Path, checksum: Path) -> bool:
        return (path.is_file() and not path.is_symlink()
                and checksum.is_file() and not checksum.is_symlink())

    @staticmethod
    def _remove_incomplete_pair(path: Path, checksum: Path) -> None:
        if DiskBlockStore._complete_regular_pair(path, checksum):
            return
        for candidate in (path, checksum):
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink(missing_ok=True)

    @staticmethod
    def _secure_directory(path: Path, *, parents: bool = False) -> None:
        if path.is_symlink():
            raise OSError(f"refusing symlink cache directory: {path}")
        path.mkdir(parents=parents, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"invalid cache directory: {path}")
        os.chmod(path, 0o700)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _checksum_path(path: Path) -> Path:
        return path.with_name(path.name + ".sha256")

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
