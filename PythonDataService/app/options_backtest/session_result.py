"""One session of a run, as it is kept (#3030).

A session's result holds the replay's outcome untouched, the cash the session started and ended with, and the pins of
what was read to replay it.

Cash. The replay's ``cash_change_usd`` counts only cash that moved: a leg marked, not sold, has paid out and not been
paid back. The next session starts with that money back at the mark, so a session ends with
``cash_after = cash_before + cash_change + marked_value``, which is ``cash_before + pnl`` (the replay's own identity
``pnl = cash_change + marked_value``). The three figures are all kept, so a reader sees the identity as numbers.

Time. A trading date is kept as a ``date``, the key the run and its data share, and beside it as the session's open and
close in ``int64 ms UTC``, the form anything stored or sent uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.results import SessionOutcome, SessionStatus


@dataclass(frozen=True)
class PinnedDay:
    """One contract's day of bars, and the sha256 of the stored file it was read from."""

    contract: OptionContract
    sha256: str


@dataclass(frozen=True)
class SessionPins:
    """What was read to replay a session. All empty for a session skipped on its window, which reads nothing."""

    underlying_sha256: str | None
    listing_sha256: str | None
    # The contracts the session could trade, in the order of their OCC symbols.
    option_days: tuple[PinnedDay, ...]


@dataclass(frozen=True)
class SessionResult:
    """One session replayed in a run."""

    trading_date: date
    session_open_ms: int
    session_close_ms: int
    # The cash the session started with, a mark from the session before already paid back.
    cash_before_usd: Decimal
    outcome: SessionOutcome
    # The cash the next session starts with: before, plus the cash that moved, plus the marks paid back.
    cash_after_usd: Decimal
    pins: SessionPins

    @property
    def status(self) -> SessionStatus:
        return self.outcome.status

    @property
    def pnl_usd(self) -> Decimal:
        """The session's profit, a leg marked, not sold, counted at its mark."""
        return self.outcome.pnl_usd

    @property
    def fees_usd(self) -> Decimal:
        """Every fee the session's trades paid, entries and exits."""
        with localcontext(fills.EXACT):
            return sum((trade.entry_fee_usd + trade.exit_fee_usd for trade in self.outcome.trades), start=Decimal(0))
