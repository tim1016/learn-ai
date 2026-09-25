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
  unsealed binding has no attested decision clock, so it gets no policy. The
  decision session itself is resolved at the run's boundary and handed in
  already proven coherent (:class:`~app.services.decision_session.RunDecisionSession`),
  so this module has no "extended run without a window" case of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.marketdata.feed import (
    DELIVERY_ALLOWANCE_MS,
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataBar,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
    record_continuity_event,
)
from app.services.decision_clock import decision_timeframe_ms_for_binding
from app.services.decision_session import RunDecisionSession
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    # Type-only for the same reason ``decision_clock`` guards this import:
    # a policy author has no business dragging the broker/clerk stack in.
    from app.services.bot_binding_repository import BrokerBotBinding
    from app.services.source_bar_ledger import SourceBarLedger

logger = logging.getLogger(__name__)

DECISION_LATE_REASON_CODE = "DECISION_LATE"
"""Why a decision bar was refused for arriving after its delivery allowance --
the continuity refusal's ``reason`` and the runner's ``blocked`` receipt code."""


class FeedContinuityRefused(MarketDataFeedError):
    """A recovered bar this run cannot accept as a decision input.

    Fatal like any other ``MarketDataFeedError`` -- the run ends rather than
    deciding on it -- but ``reason`` is always set, so the duty outcome says
    which continuity rule refused it instead of reporting a bare feed death.
    """

    reason: str

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, reason=reason)


@dataclass(frozen=True)
class LateDecision:
    """A decision bar observed further past its close than the delivery allowance."""

    observed_at_ms: int
    lateness_ms: int
    allowance_ms: int


def late_decision(
    policy: ContinuityPolicy | None,
    decision_bar_close_ms: int,
    *,
    observed_at_ms: int | None = None,
) -> LateDecision | None:
    """Whether deciding on a bar that closed at ``decision_bar_close_ms`` *now* is too late.

    The one lateness rule on the decision path, read against the one clock
    (``now_ms_utc``) and the one allowance (the run's policy, or
    ``DELIVERY_ALLOWANCE_MS`` when the run has none). Provenance plays no
    part: a minute held by the assembler until the next print, or a bucket a
    reconnect delivered late, is as stale as the wall clock says it is,
    whichever connection produced it (#2303, #2345). ``observed_at_ms`` judges
    an earlier instant instead of now -- only delivery admission passes it.
    """
    allowance_ms = DELIVERY_ALLOWANCE_MS if policy is None else policy.delivery_allowance_ms
    observed_at_ms = now_ms_utc() if observed_at_ms is None else observed_at_ms
    lateness_ms = observed_at_ms - decision_bar_close_ms
    if lateness_ms <= allowance_ms:
        return None
    return LateDecision(observed_at_ms=observed_at_ms, lateness_ms=lateness_ms, allowance_ms=allowance_ms)


async def admit_on_delivery(
    policy: ContinuityPolicy | None, bar: MarketDataBar, *, delivered_at_ms: int | None = None
) -> None:
    """Refuse a recovered decision bar that arrived after its allowance.

    A bar assembled across an interruption is a real decision input, so it is
    admitted on the same terms as any other -- except for *when* it arrived.
    Delivery time is what a reconnect distorts: if the consumer's trigger for
    this close is already past by more than the policy's allowance, deciding on
    it now would be deciding against a market that has since moved. The refusal
    is recorded before it is raised, so the run's own evidence explains the
    outcome, and it is fatal because the reconnect that produced the bar broke
    the continuity contract this run was promised.

    A ``realtime`` bar is not judged here, but it is not exempt from
    lateness: one connection can still hand over a late minute (the assembler
    holds a minute until the next print arrives, #2345). It carries no
    continuity fault to record, so its lateness is judged where every decision
    is, at the runner's custody boundary, by :func:`late_decision` -- the same
    rule this function applies. A bar the consumer does not decide on cannot
    be a late decision.

    ``delivered_at_ms`` is when the stream delivered a bar the run held while
    it prepared (#2410): that bar is admitted on the terms it arrived on, not
    faulted for the preparation the run chose. Whether the run may still
    *decide* on it is the runner's per-decision lateness gate, as for any bar.
    """
    if policy is None or bar.provenance == "realtime" or not policy.is_trigger_ms(bar.end_ms):
        return
    late = late_decision(policy, bar.end_ms, observed_at_ms=delivered_at_ms)
    if late is None:
        return
    # Through the same typed wrapper the feed writes with: a sink that cannot
    # take this refusal is CONTINUITY_EVIDENCE_UNWRITABLE, not a bare OSError
    # escaping the port on the way to the run's outcome.
    await record_continuity_event(
        policy,
        FeedContinuityEvent(
            kind="refused",
            feed_id=bar.feed_id,
            symbol=bar.symbol,
            observed_at_ms=late.observed_at_ms,
            reason=DECISION_LATE_REASON_CODE,
            window_start_ms=bar.start_ms,
            window_end_ms=bar.end_ms,
            bar_identity=f"{bar.feed_id}:{bar.symbol}:{bar.start_ms}:{bar.end_ms}",
        ),
    )
    raise FeedContinuityRefused(
        f"trigger bar {bar.start_ms}..{bar.end_ms} delivered after the allowance",
        reason=DECISION_LATE_REASON_CODE,
    )


def _refuse_every_substitution(
    window_start_ms: int, window_end_ms: int
) -> SubstitutionGrant | SubstitutionRefusal:
    """Refuse to authorize backfill of any missed window; see the module docstring."""
    del window_start_ms, window_end_ms
    return SubstitutionRefusal(reason="SUBSTITUTION_NOT_AUTHORIZED")


def continuity_policy_for(
    binding: BrokerBotBinding, ledger: SourceBarLedger, *, session: RunDecisionSession
) -> ContinuityPolicy | None:
    """Return the continuity contract for this run, or ``None`` when there is none.

    ``ledger`` is the run's own source-bar ledger; continuity events are
    journalled into it under ``binding.run_id``, so a receipt can order them
    against the bars the same run retained. ``session`` is the run's already
    resolved decision session — the run's boundary refused the deploy if it
    could not resolve one, so there is no unknown-window case here.
    """
    if binding.sealed_program is None:
        return _not_offered(binding, reason="unsealed_binding")
    timeframe_ms = decision_timeframe_ms_for_binding(binding)
    if timeframe_ms is None:
        return _not_offered(binding, reason="no_decision_timeframe")

    run_id = binding.run_id

    async def _sink(event: FeedContinuityEvent) -> ContinuityEventRef:
        return ledger.append_event(event, run_id=run_id)

    return ContinuityPolicy(
        session=session,
        next_trigger_ms=session.next_trigger_function(timeframe_ms),
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
