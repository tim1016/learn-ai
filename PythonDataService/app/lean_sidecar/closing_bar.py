"""The closing-bar rule (#2607): one predicate beside the canonical calendar.

A decision bucket is the session's *closing bar* when its close equals the
scheduled NYSE close for its date -- the regular close, or an early close's own
-- exactly as :mod:`app.lean_sidecar.trading_calendar` states it. Live decides
that bucket only after the market has closed, so it cannot trade it. Neither
runtime acts on such a decision:

* live (``app.services.bot_trade_strategy``) does not send it to the Clerk;
* backtest (``app.engine.engine.BacktestEngine``) settles it DISCARD in every
  fill mode outside the LEAN-compatibility profile.

Both refuse it the same way -- ``Settlement.DISCARD``, a Signal Program's
refused-decision path -- so an ENTER is dropped, and an EXIT stays due and
fires on the next session's first decision. A live run and its backtest
therefore agree on the bar, which is the point of the rule.

This lives beside ``trading_calendar`` rather than inside it: the calendar's
bytes are sealed into every Signal Program's qualification receipt
(``app.engine.strategy.program_sources``), so a line added there would
invalidate every seal.
"""

from __future__ import annotations

from enum import StrEnum

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.utils.session_anchors import et_date_at_ms

CLOSING_BAR_REASON_CODE = "CLOSING_BAR"
"""The decision-receipt reason a live runner records for a refused closing-bar decision."""


class ClosingBarConvention(StrEnum):
    """How a run treats a Signal Program decision on the closing bar.

    Recorded on every backtest run's evidence provenance, so evidence produced
    under a different convention is classified rather than silently compared.
    """

    SKIP_CLOSING_BAR = "skip_closing_bar/v1"
    """The decision settles DISCARD, exactly as live refuses it (#2607)."""

    LEAN_NEXT_OPEN = "lean_next_open/v1"
    """The LEAN-compatibility profile: every decision commits, and one taken on
    the closing bar fills at the next session's first minute, as LEAN fills it."""


def is_closing_bar(bar_close_ms: int) -> bool:
    """True iff a bucket closing at ``bar_close_ms`` is its session's closing bar.

    ``bar_close_ms`` is the bucket's close as ``int64 ms UTC``. The session is
    the New York date containing that instant; an instant on a day with no
    session is never a closing bar.
    """
    try:
        window = session_window_for_date(et_date_at_ms(bar_close_ms))
    except LookupError:
        return False
    return bar_close_ms == window.close_ms_utc
