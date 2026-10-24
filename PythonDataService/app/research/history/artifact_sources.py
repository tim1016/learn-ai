"""The StrategySpec research pipeline's four file stores as History stores.

``runs/`` holds one ledger per run, and ``walk-forward/``, ``monte-carlo/`` and ``baselines/`` each hold a config and a
result per study (ADR 0072). These sources only read them, through the same ``ArtifactStore`` that writes them.

* A **spec run** is a ledger with no parent. The ledgers of a walk-forward's folds and of a baseline's samples are that
  study's lineage, reachable from its own detail, and are not History entries of their own.
* A Monte Carlo or baseline config does not name its instrument or window; its parent run's ledger does, so those two
  read it from there. A parent that is gone leaves the instrument and window "not recorded".
* A study's status lives in its result. Only the header of that file is read (``status``, the completion time and, for a
  walk-forward, the per-fold status), and it is kept per file and stamp, so a listing reads each result once.

A directory whose config cannot be parsed is not an entry; it is counted in ``unreadable`` so the page can say so. A
config that parses but whose result cannot is an entry with status ``unreadable``: it is there, and its outcome is not
known.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.research.artifact import ArtifactDescriptor, ArtifactStore
from app.research.baselines.descriptor import BASELINES_ARTIFACT
from app.research.baselines.result import BaselineConfig
from app.research.history.contract import HistoryQuery, SourceRead, SourceUnavailableError
from app.research.monte_carlo.descriptor import MONTE_CARLO_ARTIFACT
from app.research.monte_carlo.result import MonteCarloConfig
from app.research.runs.descriptor import RUNS_ARTIFACT
from app.research.runs.ledger import RunLedger
from app.research.walk_forward.descriptor import WALK_FORWARD_ARTIFACT
from app.research.walk_forward.result import WalkForwardConfig
from app.schemas.research_history import (
    HistoryStatus,
    HistoryType,
    ResearchHistoryEntry,
    ResearchHistoryRef,
    ResearchHistoryWindow,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS

logger = logging.getLogger(__name__)

_KEPT_FILES = 16_384


def _instant(value: int, *, name: str) -> int:
    if not 0 <= value <= MAX_TIMESTAMP_MS:
        raise ValueError(f"{name} {value} is not an instant this domain admits")
    return value


@dataclass(frozen=True, slots=True)
class _Settled:
    """How a record ended."""

    status: HistoryStatus
    detail: str | None
    completed_at_ms: int | None

    def __post_init__(self) -> None:
        if self.completed_at_ms is not None:
            _instant(self.completed_at_ms, name="completed_at_ms")


@dataclass(frozen=True, slots=True)
class _Own:
    """What one config file says about its record. Small, so one is kept per file."""

    native_id: str
    created_at_ms: int
    question: str
    provenance: str | None
    parent_run_id: str | None
    symbols: tuple[str, ...]
    """Empty when the config does not name its instrument."""
    window: tuple[int, int] | None
    settled: _Settled | None
    """Set when the config itself says how the record ended (a ledger); otherwise the result says."""
    headline: str | None = None
    """One line of the result, for a store whose config holds one."""

    def __post_init__(self) -> None:
        _instant(self.created_at_ms, name="created_at_ms")
        if self.window is not None:
            for edge in self.window:
                _instant(edge, name="window")


class _Unreadable:
    """A stamp of a file that did not parse; kept so a bad file is parsed once."""


_UNREADABLE = _Unreadable()


@lru_cache(maxsize=_KEPT_FILES)
def _read_stamped[T](reader: Callable[[str], T], path: str, mtime_ns: int, size: int) -> T | _Unreadable:
    """``reader(path)``, kept for this file at this stamp. A file that does not parse is kept as unreadable."""
    try:
        return reader(path)
    except ValueError as exc:  # JSON syntax and pydantic validation errors are both ValueErrors
        logger.warning(
            "a stored research record cannot be parsed; it is counted as unreadable",
            extra={"action": "research_history_record_unreadable", "path": path, "reason": type(exc).__name__},
        )
        return _UNREADABLE


def _stamped[T](reader: Callable[[str], T], path: Path) -> T | _Unreadable | None:
    """The cached read of ``path``, or ``None`` when it was removed meanwhile. Other OS errors propagate."""
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return None
    return _read_stamped(reader, str(path), stat.st_mtime_ns, stat.st_size)


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _counted(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _spec_label(*names: object) -> str:
    return next((str(name) for name in names if name), "unnamed spec")


# ── What each config says ───────────────────────────────────────────────────────────────────────


def _own_of_ledger(path: str) -> _Own:
    ledger = RunLedger.model_validate_json(_read_text(path))
    label = _spec_label(ledger.strategy_spec_id, ledger.strategy_spec_json.get("name"))
    return _Own(
        native_id=ledger.run_id,
        created_at_ms=ledger.created_at_ms,
        question=f'How did "{label}" trade {ledger.symbol}?',
        provenance=f"data {ledger.data_snapshot_id}; engine {ledger.engine_version}",
        parent_run_id=ledger.parent_run_id,
        symbols=(ledger.symbol,),
        window=(ledger.start_ms, ledger.end_ms),
        settled=_Settled(ledger.status, ledger.failure_reason, ledger.completed_at_ms),
    )


def _own_of_walk_forward(path: str) -> _Own:
    config = WalkForwardConfig.model_validate_json(_read_text(path))
    label = _spec_label(config.strategy_spec_json.get("name"), config.strategy_spec_hash[:12])
    protocol = f"; protocol {config.protocol_id} {config.protocol_version}" if config.protocol_id else ""
    return _Own(
        native_id=config.walk_forward_id,
        created_at_ms=config.created_at_ms,
        question=f'Did "{label}" hold up out of sample on {config.symbol}?',
        provenance=f"spec {config.strategy_spec_hash[:12]}; seed {config.random_seed}{protocol}",
        parent_run_id=config.parent_run_id,
        symbols=(config.symbol,),
        window=(config.start_ms, config.end_ms),
        settled=None,
    )


def _own_of_monte_carlo(path: str) -> _Own:
    config = MonteCarloConfig.model_validate_json(_read_text(path))
    return _Own(
        native_id=config.monte_carlo_id,
        created_at_ms=config.created_at_ms,
        question=(
            f"What range of results do run {config.parent_run_id[:8]}'s trades allow? "
            f"({config.method}, {_counted(config.simulation_count, 'path')})"
        ),
        provenance=f"parent trade log {config.parent_trade_log_hash[:12]}; seed {config.random_seed}",
        parent_run_id=config.parent_run_id,
        symbols=(),
        window=None,
        settled=None,
    )


def _own_of_baseline(path: str) -> _Own:
    config = BaselineConfig.model_validate_json(_read_text(path))
    method = config.method.replace("_", " ")
    return _Own(
        native_id=config.baseline_id,
        created_at_ms=config.created_at_ms,
        question=(
            f"How does run {config.parent_run_id[:8]} compare with a {method} baseline? "
            f"({_counted(config.sample_count, 'sample')})"
        ),
        provenance=f"parent trade log {config.parent_trade_log_hash[:12]}; seed {config.random_seed}",
        parent_run_id=config.parent_run_id,
        symbols=(),
        window=None,
        settled=None,
    )


# ── What each result's header says ──────────────────────────────────────────────────────────────


class _ResultHeader(BaseModel):
    """The few fields of a result file the index needs; the rest of the result is the owner's."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["completed", "failed"]
    completed_at_ms: int | None = None
    failure_reason: str | None = None


class _FoldHeader(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["completed", "failed"] = "completed"


class _WalkForwardHeader(_ResultHeader):
    folds: list[_FoldHeader] = []
    selection_failures: list[object] = []


def _settled_by_header(path: str) -> _Settled:
    header = _ResultHeader.model_validate_json(_read_text(path))
    return _Settled(header.status, header.failure_reason, header.completed_at_ms)


def _settled_by_walk_forward_header(path: str) -> _Settled:
    header = _WalkForwardHeader.model_validate_json(_read_text(path))
    if header.status == "failed":
        return _Settled("failed", header.failure_reason, header.completed_at_ms)
    failed_folds = sum(fold.status == "failed" for fold in header.folds)
    unselected = len(header.selection_failures)
    if failed_folds == 0 and unselected == 0:
        return _Settled("completed", None, header.completed_at_ms)
    parts = []
    if failed_folds:
        parts.append(f"{failed_folds} of {len(header.folds)} folds failed")
    if unselected:
        parts.append(f"{unselected} had no parameters selected")
    return _Settled("partial", "; ".join(parts), header.completed_at_ms)


_RESULT_UNREADABLE = _Settled("unreadable", "its result file cannot be read", None)


# ── The sources ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Candidate:
    own: _Own
    config_path: Path
    symbols: tuple[str, ...]
    window: tuple[int, int] | None


class _ArtifactSource:
    """A file store read through its descriptor. A subclass says which store, how its configs read and how it settles."""

    type: HistoryType
    label: str
    descriptor: ArtifactDescriptor
    own_reader: Callable[[str], _Own]
    result_reader: Callable[[str], _Settled] | None = None
    """``None``: the config says how the record ended."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    def order_key(self, native_id: str) -> str:
        return native_id

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        try:
            return await asyncio.to_thread(self._read, query, take)
        except OSError as exc:
            raise SourceUnavailableError("its files could not be read") from exc

    def _store(self) -> ArtifactStore:
        return ArtifactStore(self.descriptor, root=self._root)

    def _includes(self, own: _Own) -> bool:
        return True

    def _placed(self, own: _Own, parents: dict[str, _Own | None]) -> tuple[tuple[str, ...], tuple[int, int] | None]:
        """The instrument and window of a record. By default the config's own."""
        return own.symbols, own.window

    def _wants_parents(self) -> bool:
        return False

    def _parent_of(self, own: _Own) -> ResearchHistoryRef | None:
        if own.parent_run_id is None:
            return None
        return ResearchHistoryRef(type="spec-run", native_id=own.parent_run_id)

    def _read(self, query: HistoryQuery, take: int) -> SourceRead:
        found, unreadable = self._owns()
        owns = [(own, path) for own, path in found if self._includes(own)]
        parent_ids = {own.parent_run_id for own, _ in owns if own.parent_run_id}
        parents = self._parent_ledgers(parent_ids) if self._wants_parents() else {}
        candidates = [_Candidate(own, path, *self._placed(own, parents)) for own, path in owns]
        in_time = [c for c in candidates if query.admits_time(c.own.created_at_ms)]
        symbols = frozenset(symbol for c in in_time for symbol in c.symbols)
        matching = [c for c in in_time if query.symbol is None or query.symbol in c.symbols]
        matching.sort(key=lambda c: (-c.own.created_at_ms, c.own.native_id))

        if query.status is None:
            settled = {c.own.native_id: self._settle(c) for c in matching[:take]}
            counted = matching
        else:
            settled = {c.own.native_id: self._settle(c) for c in matching}
            counted = [c for c in matching if settled[c.own.native_id].status == query.status]
        entries = tuple(self._entry(c, settled[c.own.native_id]) for c in counted[:take])
        return SourceRead(total=len(counted), entries=entries, symbols=symbols, unreadable=unreadable)

    def _owns(self) -> tuple[list[tuple[_Own, Path]], int]:
        """Every readable config of the store with its path, and how many configs could not be read."""
        owns: list[tuple[_Own, Path]] = []
        unreadable = 0
        for _artifact_id, config_path in self._store().config_paths():
            own = _stamped(self.own_reader, config_path)
            if own is None:
                continue
            if isinstance(own, _Unreadable):
                unreadable += 1
            else:
                owns.append((own, config_path))
        return owns, unreadable

    def _parent_ledgers(self, parent_ids: set[str]) -> dict[str, _Own | None]:
        """The ledgers a set of runs name as their parent; ``None`` for one that is gone or unreadable."""
        paths = dict(ArtifactStore(RUNS_ARTIFACT, root=self._root).config_paths())
        found: dict[str, _Own | None] = {}
        for parent_id in parent_ids:
            path = paths.get(parent_id)
            ledger = None if path is None else _stamped(_own_of_ledger, path)
            found[parent_id] = None if ledger is None or isinstance(ledger, _Unreadable) else ledger
        return found

    def _settle(self, candidate: _Candidate) -> _Settled:
        if candidate.own.settled is not None:
            return candidate.own.settled
        if self.result_reader is None:
            raise AssertionError(f"{self.type}: neither the config nor a result reader says how a record ended")
        settled = _stamped(self.result_reader, candidate.config_path.parent / self.descriptor.result_filename)
        return _RESULT_UNREADABLE if settled is None or isinstance(settled, _Unreadable) else settled

    def _entry(self, candidate: _Candidate, settled: _Settled) -> ResearchHistoryEntry:
        own = candidate.own
        return ResearchHistoryEntry(
            type=self.type,
            native_id=own.native_id,
            question=own.question,
            symbols=list(candidate.symbols),
            window=None if candidate.window is None else ResearchHistoryWindow(start_ms=candidate.window[0], end_ms=candidate.window[1]),
            status=settled.status,
            status_detail=settled.detail,
            created_at_ms=own.created_at_ms,
            completed_at_ms=settled.completed_at_ms,
            provenance=own.provenance,
            headline=own.headline,
            parent=self._parent_of(own),
        )


class SpecRuns(_ArtifactSource):
    type: HistoryType = "spec-run"
    label = "saved spec runs"
    descriptor = RUNS_ARTIFACT
    own_reader = staticmethod(_own_of_ledger)

    def _includes(self, own: _Own) -> bool:
        return own.parent_run_id is None


class WalkForwards(_ArtifactSource):
    type: HistoryType = "walk-forward"
    label = "saved walk-forward runs"
    descriptor = WALK_FORWARD_ARTIFACT
    own_reader = staticmethod(_own_of_walk_forward)
    result_reader = staticmethod(_settled_by_walk_forward_header)


class _ChildOfARun(_ArtifactSource):
    """A study whose instrument and window are its parent run's."""

    def _wants_parents(self) -> bool:
        return True

    def _placed(self, own: _Own, parents: dict[str, _Own | None]) -> tuple[tuple[str, ...], tuple[int, int] | None]:
        parent = parents.get(own.parent_run_id) if own.parent_run_id else None
        return (parent.symbols, parent.window) if parent is not None else ((), None)


class MonteCarlos(_ChildOfARun):
    type: HistoryType = "monte-carlo"
    label = "saved Monte Carlo runs"
    descriptor = MONTE_CARLO_ARTIFACT
    own_reader = staticmethod(_own_of_monte_carlo)
    result_reader = staticmethod(_settled_by_header)


class Baselines(_ChildOfARun):
    type: HistoryType = "baseline"
    label = "saved baseline runs"
    descriptor = BASELINES_ARTIFACT
    own_reader = staticmethod(_own_of_baseline)
    result_reader = staticmethod(_settled_by_header)


__all__ = ["Baselines", "MonteCarlos", "SpecRuns", "WalkForwards"]
