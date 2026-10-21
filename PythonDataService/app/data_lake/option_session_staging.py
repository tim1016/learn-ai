"""A durable shared byte budget for option-session collection and publication.

Reconciliation scans once per collector/publication lifecycle, including old
listing generations and orphan publication temps. Individual outcomes use a
small durable ledger instead. Kernel locks identify live reservations across
processes; a crash cannot free space before its staged bytes are accounted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from itertools import chain
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.engine.live.identity import confine_path_to_root
from app.utils.advisory_lock import advisory_file_lock, try_advisory_file_lock
from app.utils.atomic_file import atomic_write_bytes

# Several worst-case 128 MiB containers fit without allowing a backfill to fill
# the host disk. Operators can raise this explicit budget after checking space.
DEFAULT_STAGING_MAX_BYTES = settings.OPTION_ARCHIVE_STAGING_MAX_BYTES
# One 8 MiB decoded ticker zip plus compression/framing and its receipt.
TICKER_RESERVATION_BYTES = 9 * 1024**2
_MAX_LEDGER_BYTES = 32 * 1024
_METADATA_BYTES = 2 * _MAX_LEDGER_BYTES + 1024
_MAX_RESERVATIONS = 128
_LEASE_NAME = re.compile(r"\.([a-f0-9]{32})\.lock")
_LEDGER_TEMP_NAME = re.compile(r"\.option_session_budget\.json\.[a-f0-9]{16}\.tmp")


class StagingCapacityError(ValueError):
    """A preservation pause, never a failed provider attempt; a capture answers it as ``staging_capacity``."""


class OptionSessionStaging:
    def __init__(self, staging_root: Path, *, max_bytes: int = DEFAULT_STAGING_MAX_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes < _METADATA_BYTES + 1:
            raise ValueError(f"option staging byte budget must be at least {_METADATA_BYTES + 1} bytes")
        self.base, self.max_bytes = staging_root, max_bytes
        self.root = self._path(staging_root / "option_sessions")
        self.publication_root = self._path(staging_root / "option_session_publications")
        self.ledger = self._path(staging_root / "option_session_budget.json")
        self.locks = self._path(staging_root / "option_session_budget_leases")
        self._ready = False

    def _path(self, path: Path) -> Path:
        confined = confine_path_to_root(path, self.base, label="option staging budget")
        current = self.base
        for part in path.relative_to(self.base).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("option staging budget paths cannot contain a symlink")
        return confined

    @contextmanager
    def _locked(self) -> Iterator[None]:
        target = self._path(self.base / "option_session_budget")
        lock = self._path(target.with_name(f".{target.name}.lock"))
        if lock.exists() and lock.stat().st_size > 1:
            raise ValueError("option staging budget lock metadata is oversized")
        with advisory_file_lock(target):
            yield

    def _load(self) -> dict:
        path = self._path(self.ledger)
        if not path.exists():
            return {"version": 1, "used_bytes": _METADATA_BYTES, "leases": {}}
        if path.stat().st_size > _MAX_LEDGER_BYTES:
            raise ValueError("option staging budget ledger is oversized")
        state = json.loads(path.read_bytes())
        if (not isinstance(state, dict) or state.get("version") != 1
                or type(state.get("used_bytes")) is not int or state["used_bytes"] < _METADATA_BYTES
                or not isinstance(state.get("leases"), dict) or len(state["leases"]) > _MAX_RESERVATIONS
                or any(len(key) != 32 or any(c not in "0123456789abcdef" for c in key)
                       or type(size) is not int or size < 0 for key, size in state["leases"].items())):
            raise ValueError("option staging budget ledger is invalid")
        return state

    def _save(self, state: dict) -> None:
        raw = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > _MAX_LEDGER_BYTES:
            raise StagingCapacityError("Archive staging capacity: reservation metadata is full; retry after collectors drain.")
        atomic_write_bytes(self._path(self.ledger), raw)

    def _lease_target(self, token: str) -> Path:
        target = self._path(self.locks / token)
        self._path(target.with_name(f".{target.name}.lock"))
        return target

    def _measure(self) -> int:
        def vanished_directory(exc: OSError) -> None:
            if not isinstance(exc, FileNotFoundError):
                raise exc

        def walk(root: Path) -> Iterator[Path]:
            if root.is_symlink():
                self._path(root)
                raise ValueError("option staging budget cannot account a symlink")
            for folder, directories, files in os.walk(root, onerror=vanished_directory):
                for name in chain(directories, files):
                    yield Path(folder) / name

        # Stock and per-contract option writers keep their existing independent policy.
        paths = chain(walk(self.root), walk(self.publication_root))
        used = _METADATA_BYTES
        for path in paths:
            self._path(path)
            try:
                info = path.lstat()
            except FileNotFoundError:
                # A live publisher can promote its temp outside this lock.
                # Its reservation is still charged; absence can only free bytes.
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("option staging budget cannot account a symlink")
            if stat.S_ISREG(info.st_mode):
                used += info.st_size
        return used

    def _reconcile(self, state: dict) -> None:
        for path in self.base.glob(".option_session_budget.json.*.tmp"):
            checked = self._path(path)
            if not _LEDGER_TEMP_NAME.fullmatch(path.name) or not checked.is_file() or checked.stat().st_size > _MAX_LEDGER_BYTES:
                raise ValueError("option staging budget has unrecognized atomic ledger metadata")
            checked.unlink()
        for token in tuple(state["leases"]):
            with try_advisory_file_lock(self._lease_target(token)) as dead:
                if dead:
                    del state["leases"][token]
        # Only budget-lock metadata is removed. Retained outcomes and abandoned
        # listing/publication generations are never discarded to obtain space.
        for path in self.locks.iterdir() if self.locks.exists() else ():
            checked = self._path(path)
            matched = _LEASE_NAME.fullmatch(path.name)
            if matched is None or not checked.is_file() or checked.stat().st_size != 1:
                raise ValueError("option staging budget contains unrecognized reservation metadata")
            token = matched[1]
            if token not in state["leases"]:
                checked.unlink()
        state["used_bytes"] = self._measure()
        # A live publication's temp and its outstanding reservation may both
        # be counted until promotion/drain. That conservative headroom is freed
        # by its final reconciliation, never by guessing that it has finished.
        self._ready = True

    def reconcile(self) -> None:
        with self._locked():
            state = self._load()
            self._reconcile(state)
            self._save(state)

    def _require_space(self, state: dict, additional: int) -> None:
        if state["used_bytes"] + sum(state["leases"].values()) + additional > self.max_bytes:
            raise StagingCapacityError(
                f"Archive staging capacity: retained files and live reservations need more than {self.max_bytes} bytes. "
                "Verified progress is retained. Review free disk space, then raise OPTION_ARCHIVE_STAGING_MAX_BYTES "
                "and retry; do not delete uncommitted outcomes to make room.")

    @contextmanager
    def collector(self, target: Path) -> Iterator[bool]:
        """Account the stable collector lock before creating it; ``False`` when another collector holds it.

        A budget that cannot hold the lock raises :class:`StagingCapacityError` before anything is yielded.
        """
        target = confine_path_to_root(target, self.root, label="option capture collector")
        lock = self._path(target.with_name(f".{target.name}.lock"))
        with ExitStack() as stack:
            with self._locked():
                state = self._load()
                self._reconcile(state)
                new = not lock.exists()
                self._require_space(state, int(new))
                owned = stack.enter_context(try_advisory_file_lock(target))
                state["used_bytes"] += int(new)
                self._save(state)
            yield owned

    def reserve(self, size: int) -> StagingReservation:
        if type(size) is not int or size < 0:
            raise ValueError("staging reservation must be nonnegative")
        if not self._ready:
            self.reconcile()
        stack = ExitStack()
        try:
            with self._locked():
                state = self._load()
                self._require_space(state, size)
                if len(state["leases"]) >= _MAX_RESERVATIONS:
                    raise StagingCapacityError("Archive staging capacity: too many active collectors; retry after they drain.")
                token = uuid4().hex
                # An unlocked lease reads as dead to the next reconciliation, which would free live bytes.
                if not stack.enter_context(try_advisory_file_lock(self._lease_target(token))):
                    raise RuntimeError("a new staging reservation could not lock its own lease")
                state["leases"][token] = size
                self._save(state)
            return StagingReservation(self, token, stack)
        except BaseException:
            stack.close()
            raise

    def clear_committed(self, generation: Path) -> None:
        with self._locked():
            generation = confine_path_to_root(generation, self.root, label="option capture cleanup")
            if generation.is_dir():
                shutil.rmtree(generation)
            state = self._load()
            self._reconcile(state)
            self._save(state)


class StagingReservation:
    def __init__(self, budget: OptionSessionStaging, token: str, stack: ExitStack) -> None:
        self.budget, self.token, self.stack = budget, token, stack
        self.uncertain = False

    def write(self, files: Mapping[Path, bytes]) -> None:
        budget = self.budget
        with budget._locked():
            state = budget._load()
            used, peak = state["used_bytes"], state["used_bytes"]
            checked = {confine_path_to_root(path, budget.root, label="option capture staged file"): raw
                       for path, raw in files.items()}
            for path, raw in checked.items():
                peak = max(peak, used + len(raw))
                used += len(raw) - (path.stat().st_size if path.exists() else 0)
            if peak - state["used_bytes"] > state["leases"][self.token]:
                raise StagingCapacityError("Archive staging capacity: this outcome exceeds its reserved byte bound.")
            try:
                for path, raw in checked.items():
                    atomic_write_bytes(path, raw)
                state["leases"][self.token] -= used - state["used_bytes"]
                state["used_bytes"] = used
                budget._save(state)
            except BaseException:
                self.uncertain = True
                raise

    def close(self, *, reconcile: bool = False) -> None:
        try:
            with self.budget._locked():
                state = self.budget._load()
                if reconcile or self.uncertain:
                    self.budget._reconcile(state)
                state["leases"].pop(self.token, None)
                self.budget._save(state)
                self.stack.close()
                self.budget._path(self.budget._lease_target(self.token).with_name(f".{self.token}.lock")).unlink()
        finally:
            self.stack.close()
