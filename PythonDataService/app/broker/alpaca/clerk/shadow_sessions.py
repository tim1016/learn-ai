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


def _corrupt(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Shadow session journal corrupt at {path}: {detail}")


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

    def append(self, *, kind: RowKind, session_open_ms: int, observed_at_ms: int, verdict: str) -> ShadowSessionRow:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self.path):
            row = ShadowSessionRow(
                seq=self._rows.allocate_seq(),
                kind=kind,
                session_open_ms=session_open_ms,
                observed_at_ms=observed_at_ms,
                verdict=verdict,
            )
            self._rows.append(row)
            return row

    def day_state(self, session_open_ms: int) -> ShadowDayState:
        rows = [row for row in self.rows() if row.session_open_ms == session_open_ms]
        opened = next((row.observed_at_ms for row in rows if row.kind == "day_opened"), None)
        return ShadowDayState(
            opened_at_ms=opened,
            closed_clean=any(row.kind == "session_closed_clean" for row in rows),
            non_clean_verdicts=tuple(row.verdict for row in rows if row.kind == "non_clean"),
        )

    def completed_session_opens(self) -> tuple[int, ...]:
        opens = sorted({row.session_open_ms for row in self.rows()})
        return tuple(open_ms for open_ms in opens if self.day_state(open_ms).complete)


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
        state = self._ledger.day_state(open_ms)
        if state.opened_at_ms is None:
            self._ledger.append(kind="day_opened", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
        if result.verdict != "clean":
            last = self._last_row_for(open_ms)
            if last is None or last.kind != "non_clean" or last.verdict != result.verdict:
                self._ledger.append(kind="non_clean", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
            return result
        if now_ms >= self._close_ms(day) and not state.closed_clean:
            self._ledger.append(kind="session_closed_clean", session_open_ms=open_ms, observed_at_ms=now_ms, verdict=result.verdict)
        return result

    def _close_ms(self, day: date) -> int:
        if self._window is None:
            return session_close_ms_utc(day)
        bounds = declared_session_bounds(day, self._window)
        assert bounds is not None  # a trading day by construction
        return bounds.close_ms

    def _last_row_for(self, session_open_ms: int) -> ShadowSessionRow | None:
        return next(
            (row for row in reversed(self._ledger.rows()) if row.session_open_ms == session_open_ms),
            None,
        )


__all__ = [
    "SHADOW_SESSIONS_FILENAME",
    "ShadowDayState",
    "ShadowSessionLedger",
    "ShadowSessionRecorder",
    "ShadowSessionRow",
]
