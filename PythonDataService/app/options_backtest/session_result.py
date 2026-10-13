"""One session of a run, as it is kept (#3030).

A session's result holds the replay's outcome untouched, the cash the session started and ended with, and the pins of
what was read to replay it.

Cash. The replay's ``cash_change_usd`` counts only cash that moved: a leg marked, not sold, has paid out and not been
paid back. The next session starts with that money back at the mark, and a sold leg marked, not bought back, has been
paid and owes its mark, which the next session starts without (the marked value is signed). So a session ends with
``cash_after = cash_before + cash_change + marked_value``, which is ``cash_before + pnl`` (the replay's own identity
``pnl = cash_change + marked_value``). The three figures are all kept, so a reader sees the identity as numbers.

Time. A session is named by its open and close in ``int64 ms UTC``, the form anything stored or sent uses; no ``date``
is kept. The open is the trading date's 09:30 New York anchor, so ``trading_date`` is derived from it when code that
groups by day needs the date, and is not a field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.results import SessionOutcome, SessionStatus
from app.utils.session_anchors import et_date_at_ms


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

    # The session's identity: the calendar's open of the trading date, 09:30 New York.
    session_open_ms: int
    session_close_ms: int
    # The cash the session started with, a mark from the session before already paid back.
    cash_before_usd: Decimal
    outcome: SessionOutcome
    # The cash the next session starts with: before, plus the cash that moved, plus the marks paid back.
    cash_after_usd: Decimal
    pins: SessionPins

    @property
    def trading_date(self) -> date:
        """The New York calendar date of the session's open: derived on each call, never stored or sent.

        For code that groups or looks up by day inside one function. Anything kept or served carries the open instead.
        """
        return et_date_at_ms(self.session_open_ms)

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
