"""The frozen data snapshot every cell of a sweep reads.

Formula: at launch, every session artifact the run will open — one
``{YYYYMMDD}_trade.zip`` per expected trading session for minute data, or
the per-symbol history zip for daily data — is resolved through the same
root order the readers use and fingerprinted (sha256 of the bytes on disk),
together with the calendar package version and identity that decide which
sessions exist. Engine reads are then BOUND to that manifest: the manifest
readers below hash the bytes they are about to parse and refuse any file
whose digest is not the one receipted, so no completed cell can consume
unreceipted bytes. Periodic re-verification is a diagnostic, not the
guarantee — a lake refresh between two checks, reverted before the next,
passes every check while the study consumed two datasets (review F05).
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926
  "Reproducibility receipt"; ``docs/architecture/adrs/0022`` for why the
  calendar identity is part of the snapshot.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_snapshot.py.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pandas_market_calendars

from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.runs.hashing import hash_payload

CALENDAR_IDENTITY = "NYSE"
Resolution = Literal["minute", "daily"]


class DataSnapshotIncompleteError(ValueError):
    """The lake is missing sessions the snapshot must cover."""

    def __init__(self, symbol: str, missing: Sequence[date]) -> None:
        self.symbol = symbol
        self.missing = tuple(missing)
        shown = ", ".join(day.isoformat() for day in self.missing[:10])
        more = f" (+{len(self.missing) - 10} more)" if len(self.missing) > 10 else ""
        super().__init__(f"{symbol}: {len(self.missing)} expected session(s) missing from the lake: {shown}{more}")


class DataSnapshotMismatchError(RuntimeError):
    """Bytes about to be read are not the bytes the snapshot receipted."""


@dataclass(frozen=True)
class DataSnapshot:
    symbol: str
    resolution: Resolution
    data_start: date
    data_end: date
    sessions: tuple[date, ...]
    # Root-relative artifact path -> sha256 of its bytes at capture.
    artifacts: dict[str, str] = field(default_factory=dict)
    calendar_identity: str = CALENDAR_IDENTITY
    calendar_version: str = pandas_market_calendars.__version__

    def digest(self) -> str:
        return hash_payload(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "resolution": self.resolution,
            "data_start": self.data_start.isoformat(),
            "data_end": self.data_end.isoformat(),
            "sessions": [day.isoformat() for day in self.sessions],
            "artifacts": dict(sorted(self.artifacts.items())),
            "calendar_identity": self.calendar_identity,
            "calendar_version": self.calendar_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DataSnapshot:
        return cls(
            symbol=str(payload["symbol"]),
            resolution=payload["resolution"],
            data_start=date.fromisoformat(payload["data_start"]),
            data_end=date.fromisoformat(payload["data_end"]),
            sessions=tuple(date.fromisoformat(day) for day in payload["sessions"]),
            artifacts=dict(payload["artifacts"]),
            calendar_identity=str(payload["calendar_identity"]),
            calendar_version=str(payload["calendar_version"]),
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minute_relative(symbol: str, day: date) -> str:
    return f"equity/usa/minute/{symbol.lower()}/{day.strftime('%Y%m%d')}_trade.zip"


def _daily_relative(symbol: str) -> str:
    return f"equity/usa/daily/{symbol.lower()}.zip"


def _first_existing(roots: Sequence[Path], relative: str) -> Path | None:
    for root in roots:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


def capture_data_snapshot(
    *,
    roots: Sequence[Path],
    symbol: str,
    resolution: Resolution,
    data_start: date,
    data_end: date,
) -> DataSnapshot:
    """Fingerprint every artifact the run will read; refuse if any session is absent."""
    sessions = tuple(expected_sessions(data_start, data_end))
    artifacts: dict[str, str] = {}
    if resolution == "daily":
        relative = _daily_relative(symbol)
        path = _first_existing(roots, relative)
        if path is None:
            raise DataSnapshotIncompleteError(symbol, sessions)
        present = set(LeanDailyDataReader(list(roots)).available_dates(symbol))
        missing = [day for day in sessions if day not in present]
        if missing:
            raise DataSnapshotIncompleteError(symbol, missing)
        artifacts[relative] = _sha256(path)
    else:
        missing: list[date] = []
        for day in sessions:
            relative = _minute_relative(symbol, day)
            path = _first_existing(roots, relative)
            if path is None:
                missing.append(day)
                continue
            artifacts[relative] = _sha256(path)
        if missing:
            raise DataSnapshotIncompleteError(symbol, missing)
    return DataSnapshot(
        symbol=symbol.upper(),
        resolution=resolution,
        data_start=data_start,
        data_end=data_end,
        sessions=sessions,
        artifacts=artifacts,
    )


def verify_data_snapshot(snapshot: DataSnapshot, roots: Sequence[Path]) -> list[str]:
    """Artifacts whose bytes on disk no longer match the snapshot (diagnostic)."""
    mismatched: list[str] = []
    for relative, expected in snapshot.artifacts.items():
        path = _first_existing(roots, relative)
        if path is None or _sha256(path) != expected:
            mismatched.append(relative)
    return mismatched


def _verify_bytes(manifest: Mapping[str, str], relative: str, payload: bytes) -> None:
    expected = manifest.get(relative)
    actual = hashlib.sha256(payload).hexdigest()
    if expected is None:
        raise DataSnapshotMismatchError(f"{relative} is not part of the receipted data snapshot")
    if actual != expected:
        raise DataSnapshotMismatchError(
            f"{relative} changed since the snapshot was taken (receipted {expected[:12]}…, found {actual[:12]}…)"
        )


class ManifestBoundMinuteReader(LeanMinuteDataReader):
    """A minute reader that parses only bytes matching the receipted manifest."""

    def __init__(
        self,
        data_root: Path | str | Sequence[Path | str],
        manifest: Mapping[str, str],
        session: Literal["regular", "extended"] = "regular",
    ) -> None:
        super().__init__(data_root, session=session)
        self._manifest = dict(manifest)

    def read_day(self, symbol: str, trading_date: date) -> list[TradeBar]:
        zip_path = self._zip_path(symbol, trading_date)
        if not zip_path.exists():
            return []
        payload = zip_path.read_bytes()
        _verify_bytes(self._manifest, _minute_relative(symbol, trading_date), payload)
        return self.parse_day_zip(payload, symbol, trading_date)


class ManifestBoundDailyReader(LeanDailyDataReader):
    """A daily reader that parses only bytes matching the receipted manifest."""

    def __init__(self, data_root: Path | str | Sequence[Path | str], manifest: Mapping[str, str]) -> None:
        super().__init__(data_root)
        self._manifest = dict(manifest)

    def _read_zip(self, zip_path: Path, symbol: str) -> list[TradeBar]:
        if not zip_path.exists():
            return []
        payload = zip_path.read_bytes()
        _verify_bytes(self._manifest, _daily_relative(symbol), payload)
        return self.parse_history_zip(payload, symbol)
