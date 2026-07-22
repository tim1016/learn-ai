"""Shared crash-durable append primitives for Clerk-owned artifacts.

This module owns only filesystem ordering: write the complete row, flush the
file, fsync it, then fsync its parent directory.  Callers retain their own
schemas, replay validation, and sequence rules; conflating those domain rules
would let one ledger accidentally define another ledger's authority.

On POSIX, writes are descriptor-relative with ``O_NOFOLLOW``. Windows does
not expose the same directory-descriptor APIs to Python, so its compatibility
path requires a service-owned local artifact root: no untrusted principal may
write, rename, or create reparse points anywhere below that root. It retains
the normalise-and-recheck proof that rejects route-derived traversal and
pre-existing reparse escapes, but it cannot make the POSIX no-swap guarantee.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from app.engine.live.live_state_sidecar import _fsync_parent_dir


def append_jsonl_record(
    path: Path,
    serialized_record: str,
    *,
    trusted_root: Path,
) -> None:
    """Append exactly one durable JSONL record and fsync its directory.

    The caller must name the durable artifact root independently of ``path``.
    This prevents a path assembled from an operator-supplied identity from
    escaping the intended ledger namespace, including via a leaf symlink.
    """

    _require_single_jsonl_record(serialized_record)
    if not _supports_descriptor_relative_writes():
        _append_on_service_owned_filesystem(path, serialized_record, trusted_root)
        return
    _append_with_directory_fd(path, serialized_record, trusted_root)


def _append_with_directory_fd(path: Path, serialized_record: str, trusted_root: Path) -> None:
    with _confined_parent_directory(path, trusted_root) as (directory_fd, filename):
        file_descriptor = os.open(
            filename,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o666,
            dir_fd=directory_fd,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(serialized_record + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        _fsync_directory(directory_fd)


def _append_on_service_owned_filesystem(
    path: Path,
    serialized_record: str,
    trusted_root: Path,
) -> None:
    """Windows-compatible append under the service-owned-artifact-root contract."""

    root_real = os.path.realpath(os.fspath(trusted_root))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    with path.open("a", encoding="utf-8") as file_handle:
        file_handle.write(serialized_record + "\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    _fsync_parent_dir(path)


def rewrite_jsonl_records(
    path: Path,
    serialized_records: Iterable[str],
    *,
    trusted_root: Path,
) -> None:
    """Atomically replace a confined JSONL log after record validation."""

    records = tuple(serialized_records)
    for record in records:
        _require_single_jsonl_record(record)
    if not _supports_descriptor_relative_writes():
        _rewrite_on_service_owned_filesystem(path, records, trusted_root)
        return
    _rewrite_with_directory_fd(path, records, trusted_root)


def _rewrite_with_directory_fd(
    path: Path,
    records: tuple[str, ...],
    trusted_root: Path,
) -> None:
    with _confined_parent_directory(path, trusted_root) as (directory_fd, filename):
        temporary_name, file_descriptor = _create_exclusive_temporary_file(directory_fd, filename)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
                for record in records:
                    file_handle.write(record + "\n")
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            _fsync_directory(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


def _rewrite_on_service_owned_filesystem(
    path: Path,
    records: tuple[str, ...],
    trusted_root: Path,
) -> None:
    """Windows-compatible atomic rewrite under the service-owned-root contract."""

    root_real = os.path.realpath(os.fspath(trusted_root))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            for record in records:
                file_handle.write(record + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        candidate = os.path.realpath(os.fspath(path))
        if not candidate.startswith(root_prefix):
            raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
        path = Path(candidate)
        os.replace(temporary_path, path)
        _fsync_parent_dir(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def create_exclusive_durable_file(
    path: Path,
    serialized_record: str,
    *,
    trusted_root: Path,
    mode: int = 0o600,
) -> None:
    """Durably create one confined claim file or raise when another writer owns it."""

    if "\x00" in serialized_record:
        raise ValueError("durable record must not contain a NUL byte")
    if not _supports_descriptor_relative_writes():
        _create_exclusive_on_service_owned_filesystem(path, serialized_record, trusted_root, mode)
        return
    _create_exclusive_with_directory_fd(path, serialized_record, trusted_root, mode)


def _create_exclusive_with_directory_fd(
    path: Path,
    serialized_record: str,
    trusted_root: Path,
    mode: int,
) -> None:
    with _confined_parent_directory(path, trusted_root) as (directory_fd, filename):
        file_descriptor = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
                file_handle.write(serialized_record)
                file_handle.flush()
                os.fsync(file_handle.fileno())
        except BaseException:
            # A partial exclusive file remains a durable claim.  Deleting it could
            # permit a retry to repeat an effect that escaped before the crash.
            raise
        _fsync_directory(directory_fd)


def _create_exclusive_on_service_owned_filesystem(
    path: Path,
    serialized_record: str,
    trusted_root: Path,
    mode: int,
) -> None:
    """Windows-compatible exclusive create under the service-owned-root contract."""

    root_real = os.path.realpath(os.fspath(trusted_root))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = os.path.realpath(os.fspath(path))
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    path = Path(candidate)
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(serialized_record)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    except BaseException:
        # A partial exclusive file remains a durable claim.  Deleting it could
        # permit a retry to repeat an effect that escaped before the crash.
        raise
    _fsync_parent_dir(path)


def _require_single_jsonl_record(serialized_record: str) -> None:
    if not serialized_record or "\r" in serialized_record or "\n" in serialized_record:
        raise ValueError("a durable JSONL append must contain exactly one JSONL row")


@contextmanager
def _confined_parent_directory(path: Path, trusted_root: Path) -> Iterator[tuple[int, str]]:
    """Open ``path``'s parent from a trusted root without pathname re-traversal.

    The initial ``realpath``/prefix proof rejects an already-existing escape.
    Afterwards each directory is opened relative to its already-open parent
    with ``O_NOFOLLOW``.  A malicious swap between validation and the write
    therefore fails rather than redirecting a durable ledger operation outside
    its caller-owned namespace.
    """

    if not _supports_descriptor_relative_writes():
        raise RuntimeError("descriptor-relative durable writes are unavailable on this host")
    root_real = os.path.realpath(os.fspath(trusted_root))
    candidate = os.path.realpath(os.fspath(path))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if not candidate.startswith(root_prefix):
        raise ValueError(f"durable append log path {candidate} escapes root {root_real}")
    relative_parts = Path(os.path.relpath(candidate, root_real)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise ValueError(f"durable append log path {candidate} is not a file below {root_real}")

    Path(root_real).mkdir(parents=True, exist_ok=True)
    root_fd = _open_directory(root_real)
    directory_fd = root_fd
    try:
        for component in relative_parts[:-1]:
            next_directory_fd = _open_or_create_directory(directory_fd, component)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_directory_fd
        yield directory_fd, relative_parts[-1]
    finally:
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


def _supports_descriptor_relative_writes() -> bool:
    """Whether the host can make durable writes without a pathname-swap gap."""

    return os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")


def _open_directory(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _open_or_create_directory(parent_fd: int, component: str) -> int:
    try:
        return os.open(
            component,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        with suppress(FileExistsError):
            os.mkdir(component, 0o700, dir_fd=parent_fd)
        return os.open(
            component,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )


def _create_exclusive_temporary_file(directory_fd: int, filename: str) -> tuple[str, int]:
    """Create an unpredictable temp file inside an already-open directory."""

    for _ in range(16):
        temporary_name = f".{filename}.{secrets.token_hex(16)}.tmp"
        try:
            return temporary_name, os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate an exclusive durable temporary file")


def _fsync_directory(directory_fd: int) -> None:
    """Persist a rename or creation through the already-confined directory fd."""

    os.fsync(directory_fd)


__all__ = [
    "append_jsonl_record",
    "create_exclusive_durable_file",
    "rewrite_jsonl_records",
]
