"""Small durable-file primitives for Clerk recovery ceremonies."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.broker.alpaca.paths import fsync_directory, resolve_contained_path


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> str:
    """Atomically publish canonical JSON and return its SHA-256."""
    encoded = canonical_json_bytes(payload)
    atomic_write_bytes(path, encoded)
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode())


def atomic_write_bytes(path: Path, encoded: bytes) -> None:
    """Fsync a same-directory candidate, replace the target, then fsync its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".candidate", dir=path.parent
    )
    candidate = Path(candidate_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
        fsync_directory(path.parent)
    finally:
        candidate.unlink(missing_ok=True)


def confined_relative_path(root: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or not relative.parts:
        raise ValueError("artifact reference must be a non-empty relative path")
    return resolve_contained_path(root, *relative.parts)


def relative_reference(root: Path, path: Path) -> str:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    try:
        return path_resolved.relative_to(root_resolved).as_posix()
    except ValueError as exc:
        raise ValueError(f"{path} is outside artifacts root {root}") from exc


def tree_sha256(path: Path) -> str:
    """Hash a file or directory with names, types, and file contents bound."""
    digest = hashlib.sha256()
    if path.is_symlink():
        raise ValueError(f"refusing to hash symbolic-link artifact {path}")
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.name.encode())
        _update_file_digest(digest, path)
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"legacy artifact is not a regular file or directory: {path}")
    digest.update(b"directory\0")
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"refusing to hash symbolic link below legacy artifact: {candidate}")
        relative = candidate.relative_to(path).as_posix().encode()
        if candidate.is_dir():
            digest.update(b"dir\0" + relative + b"\0")
        elif candidate.is_file():
            digest.update(b"file\0" + relative + b"\0")
            _update_file_digest(digest, candidate)
        else:
            raise ValueError(f"unsupported legacy artifact type: {candidate}")
    return digest.hexdigest()


def _update_file_digest(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
