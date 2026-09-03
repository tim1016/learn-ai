"""Author one run's ``ContinuityPolicy`` from its sealed binding (spec #1921 §4.5).

The feed cannot know which minutes a bot decides on, what it may backfill, or
where its evidence belongs -- that is the consumer's half of the reconnect
contract, and this module is where a bot run states it.

Two properties are deliberate and fail closed:

* **Substitution is refused, always.** Handing a bot a historical bar in place
  of a live one it never saw changes what the strategy decided on. Authorizing
  that needs a counterfactual-parity artifact this plan does not build, so
  every window is refused with ``SUBSTITUTION_NOT_AUTHORIZED`` rather than
  quietly backfilled. Surviving a reconnect is about not losing the minutes
  that *were* printed, not about inventing the ones that were not.
* **A binding this module cannot describe truthfully gets no policy.** An
  unsealed binding has no attested decision clock, and an extended-hours
  binding has no calendar-proven trigger set (ruling R1). Either way the run
  keeps the pre-#1921 behavior -- no recovery, no continuity evidence -- which
  is honest, instead of being scheduled against a guessed clock.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.marketdata.feed import (
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
)
from app.services.decision_clock import (
    decision_timeframe_ms_for_binding,
    rth_next_trigger_function,
)

if TYPE_CHECKING:
    # Type-only for the same reason ``decision_clock`` guards this import:
    # a policy author has no business dragging the broker/clerk stack in.
    from app.services.bot_binding_repository import BrokerBotBinding
    from app.services.source_bar_ledger import SourceBarLedger

logger = logging.getLogger(__name__)


class FeedContinuityRefused(MarketDataFeedError):
    """A recovered bar this run cannot accept as a decision input.

    Fatal like any other ``MarketDataFeedError`` -- the run ends rather than
    deciding on it -- but ``reason`` is always set, so the duty outcome says
    which continuity rule refused it instead of reporting a bare feed death.
    """

    reason: str

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, reason=reason)


def _refuse_every_substitution(
    window_start_ms: int, window_end_ms: int
) -> SubstitutionGrant | SubstitutionRefusal:
    """Refuse to authorize backfill of any missed window; see the module docstring."""
    del window_start_ms, window_end_ms
    return SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")


def continuity_policy_for(
    binding: BrokerBotBinding, ledger: SourceBarLedger
) -> ContinuityPolicy | None:
    """Return the continuity contract for this run, or ``None`` when there is none.

    ``ledger`` is the run's own source-bar ledger; continuity events are
    journalled into it under ``binding.run_id``, so a receipt can order them
    against the bars the same run retained.
    """
    if binding.sealed_program is None:
        return _not_offered(binding, reason="unsealed_binding")
    if not binding.use_rth:
        return _not_offered(binding, reason="all_session_not_supported")
    timeframe_ms = decision_timeframe_ms_for_binding(binding)
    if timeframe_ms is None:
        return _not_offered(binding, reason="no_decision_timeframe")

    run_id = binding.run_id

    async def _sink(event: FeedContinuityEvent) -> ContinuityEventRef:
        return ledger.append_event(event, run_id=run_id)

    return ContinuityPolicy(
        decision_session="rth",
        next_trigger_ms=rth_next_trigger_function(timeframe_ms),
        substitution_grant=_refuse_every_substitution,
        record_event=_sink,
    )


def _not_offered(binding: BrokerBotBinding, *, reason: str) -> None:
    """Log why this run streams without a continuity contract, and offer none."""
    logger.info(
        "Feed continuity was not offered to this run",
        extra={
            "action": "feed_continuity_not_offered",
            "reason": reason,
            "strategy_instance_id": binding.strategy_instance_id,
            "run_id": binding.run_id,
            "symbol": binding.symbol,
        },
    )
    return None
