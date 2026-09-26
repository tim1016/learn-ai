"""One latest-known corporate-action basis per adjusted capture and reader (#2454).

The provider does not offer a revision-pinned aggregate request. Capture is
serialized per symbol on the shared lake filesystem, with actions checked on
both sides of the fetch. A changed action set invalidates the older caches
before rebuilding. Hash-bound companions make interrupted publication refuse
rather than mislabel prices. Raw data and external reference trees are untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent
from app.data_lake.types import trading_date_to_calendar_anchor_ms
from app.utils.advisory_lock import try_advisory_file_lock


class AdjustmentVersionError(ValueError):
    """Adjusted prices cannot be proved to share the latest captured basis."""


@dataclass(frozen=True)
class CorporateActionSnapshot:
    splits: tuple[SplitEvent, ...]
    dividends: tuple[DividendEvent, ...]
    effective_date: date

    def payload(self) -> bytes:
        # Vendor dates are normalized on persistence. An announced future
        # event becoming effective must change the basis even without a new row.
        def event_day(value: str) -> int:
            return trading_date_to_calendar_anchor_ms(date.fromisoformat(value))

        return json.dumps({
            "schema_version": 1,
            "splits": [[event_day(s.execution_date), s.split_from, s.split_to,
                        date.fromisoformat(s.execution_date) <= self.effective_date]
                       for s in sorted(self.splits)],
            "dividends": [[event_day(d.ex_dividend_date), d.cash_amount,
                           date.fromisoformat(d.ex_dividend_date) <= self.effective_date]
                          for d in sorted(self.dividends)],
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    @property
    def version(self) -> str:
        return hashlib.sha256(self.payload()).hexdigest()


def current_snapshot_path(symbol: str) -> PurePosixPath:
    # Symbols already passed the lake's boundary validator.
    return PurePosixPath("adjustment_versions") / f"{symbol.lower()}.json"


def companion_path(path: Path | PurePosixPath) -> Path | PurePosixPath:
    return path.with_suffix(path.suffix + ".adjustment.json")


def adjustment_companion(path: PurePosixPath, payload: bytes, version: str) -> tuple[PurePosixPath, bytes]:
    return companion_path(path), json.dumps({
        "schema_version": 1,
        "corporate_action_version": version,
        "file_sha256": hashlib.sha256(payload).hexdigest(),
    }, sort_keys=True).encode()


def current_version(root: Path, symbol: str) -> str:
    try:
        return hashlib.sha256((root / current_snapshot_path(symbol)).read_bytes()).hexdigest()
    except OSError as exc:
        raise AdjustmentVersionError(
            f"{symbol}: corporate-action version is unavailable; rebuild adjusted data before running"
        ) from exc


def adjusted_root_for(path: Path) -> Path | None:
    return next((p for p in path.parents if p.name == "polygon_split_adjusted"), None)


def verify_adjustment_receipt(
    path: Path, file_sha256: str, symbol: str, *, companion_payload: bytes | None = None,
) -> str | None:
    """Verify the version receipt against the supplied digest.

    Mode is structural in the lake layout. Legacy unversioned adjusted files
    refuse; raw roots and independent LEAN reference fixtures need no receipt.
    """
    root = adjusted_root_for(path)
    if root is None:
        return None
    try:
        record = json.loads(companion_payload if companion_payload is not None else companion_path(path).read_bytes())
        version = record["corporate_action_version"]
        valid = (record["schema_version"] == 1 and isinstance(version, str)
                 and len(version) == 64
                 and record["file_sha256"] == file_sha256
                 and version == current_version(root, symbol))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AdjustmentVersionError(
            f"{symbol}: {path.name} has no valid corporate-action version; rebuild adjusted data before running"
        ) from exc
    if not valid:
        raise AdjustmentVersionError(
            f"{symbol}: {path.name} uses a stale or mixed corporate-action version; "
            "rebuild adjusted data before running"
        )
    return version


def verify_adjusted_payload(
    path: Path, payload: bytes, symbol: str, *, companion_payload: bytes | None = None,
) -> str | None:
    """Validate the exact bytes being parsed, never a second archive read."""
    if adjusted_root_for(path) is None:
        return None
    return verify_adjustment_receipt(
        path, hashlib.sha256(payload).hexdigest(), symbol, companion_payload=companion_payload,
    )


class AdjustmentVersionGuard:
    """Reader-lifetime pin: a concurrent rebuild cannot change basis mid-run."""

    def __init__(self) -> None:
        self.versions: dict[str, str] = {}

    def verify(
        self, path: Path, payload: bytes, symbol: str, *, companion_payload: bytes | None = None,
    ) -> None:
        version = verify_adjusted_payload(path, payload, symbol, companion_payload=companion_payload)
        if version is None:
            return
        symbol = symbol.upper()
        prior = self.versions.setdefault(symbol, version)
        if version != prior:
            raise AdjustmentVersionError(
                f"{symbol}: mixed corporate-action versions during this run; retry on one rebuilt version"
            )


@asynccontextmanager
async def capture_lock(
    root: Path, symbols: Sequence[str], timeout: float, *, check_cancelled: Callable[[], None] | None = None,
) -> AsyncIterator[None]:
    """Serialize adjusted captures across processes sharing the lake volume.

    Sorted acquisition prevents a multi-symbol request deadlocking another.
    Poll nonblocking flock so an overlapping request never blocks the event loop.
    """
    from contextlib import AsyncExitStack

    @asynccontextmanager
    async def one(symbol: str) -> AsyncIterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            if check_cancelled is not None:
                check_cancelled()
            with try_advisory_file_lock(root / current_snapshot_path(symbol)) as acquired:
                if acquired:
                    yield
                    return
            if time.monotonic() >= deadline:
                raise AdjustmentVersionError(f"{symbol}: timed out waiting for the adjusted-data rebuild")
            await asyncio.sleep(0.05)

    async with AsyncExitStack() as stack:
        for symbol in sorted(set(symbols)):
            await stack.enter_async_context(one(symbol))
        yield
