"""One shadow authority's per-ET-day sweep cleanliness journal (ADR 0059 D2).

A shadow session counts only when the sweep reconciled cleanly for the whole
day. The sweep publishes a verdict every pass; this module keeps the tiny,
durable, append-only record the gate needs instead of journaling every pass:

* ``day_opened`` — the first pass observed on ET trading date D, with the
  instant it happened (the gate checks that instant preceded the instance's
  session open; a process that started at noon cannot vouch for the morning).
* ``non_clean`` — a pass on D whose verdict was not ``clean``, deduplicated
  while consecutive verdicts repeat.
* ``session_closed_clean`` — the first clean pass on D at or after the
  declared window's close (the calendar's close when there is no window).

A day is complete iff it was opened, closed clean, and never non-clean.
Dates are stored as their calendar session open in ``int64 ms UTC``
(temporal-rigor: a trading date is one ET-anchored instant, never a string).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.account_authority import require_shadow_account_id
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.services.jsonl_wal import JsonlWal
from app.services.session_authority import declared_session_bounds
from app.utils.advisory_lock import advisory_file_lock
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

SHADOW_SESSIONS_FILENAME = "shadow_sessions.jsonl"
RowKind = Literal["day_opened", "non_clean", "session_closed_clean"]


class ShadowSessionRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    kind: RowKind
    session_open_ms: int = Field(ge=0)
    observed_at_ms: int = Field(ge=0)
    verdict: str


@dataclass(frozen=True)
class ShadowDayState:
    opened_at_ms: int | None
    closed_clean: bool
    non_clean_verdicts: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.opened_at_ms is not None and self.closed_clean and not self.non_clean_verdicts


class ShadowSessionBoundsUnavailable(RuntimeError):
    """A trading day yielded no declared session bounds to close a shadow day against."""


def _corrupt(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Shadow session journal corrupt at {path}: {detail}")


def _day_state(rows: Iterable[ShadowSessionRow]) -> ShadowDayState:
    """Fold one ET day's rows into its state; ``rows`` is already that day's."""
    opened: int | None = None
    closed_clean = False
    non_clean: list[str] = []
    for row in rows:
        if row.kind == "day_opened":
            opened = row.observed_at_ms if opened is None else opened
        elif row.kind == "session_closed_clean":
            closed_clean = True
        else:
            non_clean.append(row.verdict)
    return ShadowDayState(opened_at_ms=opened, closed_clean=closed_clean, non_clean_verdicts=tuple(non_clean))


class ShadowSessionLedger:
    """Append-only journal beside one shadow authority's custody database."""

    def __init__(self, *, artifacts_root: Path, account_id: str) -> None:
        self.account_id = require_shadow_account_id(account_id)
        path = confined_account_file(artifacts_root, self.account_id, SHADOW_SESSIONS_FILENAME)
        self._rows: JsonlWal[ShadowSessionRow] = JsonlWal(
            path,
            record_model=ShadowSessionRow,
            corrupt_error=_corrupt,
            seq_of=lambda row: row.seq,
            label="shadow_session",
            trusted_root=path.parent,
        )

    @property
    def path(self) -> Path:
        return self._rows.path

    def rows(self) -> list[ShadowSessionRow]:
        return self._rows.read_all() if self.path.exists() else []

    def has_rows(self) -> bool:
        return bool(self.rows())

    @contextmanager
    def transaction(self) -> Iterator[list[ShadowSessionRow]]:
        """Hold the journal's file lock across one pass's read and its appends.

        One pass decides from what it read and may append twice; taking the
        lock per append would let a sibling writer land between the decision
        and the row it produced.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self.path):
            yield self.rows()

    def append_locked(
        self,
        rows: list[ShadowSessionRow],
        *,
        kind: RowKind,
        session_open_ms: int,
        observed_at_ms: int,
        verdict: str,
    ) -> ShadowSessionRow:
        """Append inside :meth:`transaction`; ``rows`` is that transaction's read."""
        row = ShadowSessionRow(
            seq=self._rows.allocate_seq(),
            kind=kind,
            session_open_ms=session_open_ms,
            observed_at_ms=observed_at_ms,
            verdict=verdict,
        )
        self._rows.append(row)
        rows.append(row)
        return row

    def append(self, *, kind: RowKind, session_open_ms: int, observed_at_ms: int, verdict: str) -> ShadowSessionRow:
        with self.transaction() as rows:
            return self.append_locked(
                rows,
                kind=kind,
                session_open_ms=session_open_ms,
                observed_at_ms=observed_at_ms,
                verdict=verdict,
            )

    def day_state(self, session_open_ms: int) -> ShadowDayState:
        return _day_state(row for row in self.rows() if row.session_open_ms == session_open_ms)

    def completed_session_opens(self) -> tuple[int, ...]:
        by_open: dict[int, list[ShadowSessionRow]] = {}
        for row in self.rows():
            by_open.setdefault(row.session_open_ms, []).append(row)
        return tuple(open_ms for open_ms in sorted(by_open) if _day_state(by_open[open_ms]).complete)


class ShadowSessionRecorder:
    """The sweep listener that journals each trading day's cleanliness."""

    def __init__(
        self,
        *,
        ledger: ShadowSessionLedger,
        window: ExtendedHoursWindow | None,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._ledger = ledger
        self._window = window
        self._clock = clock

    def record(self, result: AccountReconciliationResult) -> AccountReconciliationResult:
        now_ms = self._clock()
        day = et_date_at_ms(now_ms)
        if not is_trading_day(day):
            return result
        open_ms = session_open_ms_utc(day)
        # One lock for the whole pass: the day state this pass decides from and
        # the rows it appends are the same read.
        with self._ledger.transaction() as rows:
            state = _day_state(row for row in rows if row.session_open_ms == open_ms)
            if state.opened_at_ms is None:
                self._journal(rows, "day_opened", open_ms=open_ms, now_ms=now_ms, result=result)
            if result.verdict != "clean":
                last = self._last_row_for(rows, open_ms)
                if last is None or last.kind != "non_clean" or last.verdict != result.verdict:
                    self._journal(rows, "non_clean", open_ms=open_ms, now_ms=now_ms, result=result)
                return result
            if now_ms >= self._close_ms(day) and not state.closed_clean:
                self._journal(rows, "session_closed_clean", open_ms=open_ms, now_ms=now_ms, result=result)
        return result

    def _journal(
        self,
        rows: list[ShadowSessionRow],
        kind: RowKind,
        *,
        open_ms: int,
        now_ms: int,
        result: AccountReconciliationResult,
    ) -> None:
        self._ledger.append_locked(
            rows, kind=kind, session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict
        )

    def _close_ms(self, day: date) -> int:
        if self._window is None:
            return session_close_ms_utc(day)
        bounds = declared_session_bounds(day, self._window)
        if bounds is None:
            raise ShadowSessionBoundsUnavailable(
                f"{day.isoformat()} is a trading day with no declared session bounds; "
                "a shadow day cannot be closed against a window it has none of."
            )
        return bounds.close_ms

    @staticmethod
    def _last_row_for(rows: list[ShadowSessionRow], session_open_ms: int) -> ShadowSessionRow | None:
        return next((row for row in reversed(rows) if row.session_open_ms == session_open_ms), None)


__all__ = [
    "SHADOW_SESSIONS_FILENAME",
    "ShadowDayState",
    "ShadowSessionBoundsUnavailable",
    "ShadowSessionLedger",
    "ShadowSessionRecorder",
    "ShadowSessionRow",
]
