"""Operator-visible record of decision bars the sealed session refused.

``SignalProgram.on_consolidated_bar`` only *retains* a ``StageQuarantine``:
``app/engine/strategy/signal_program.py`` is listed in every registered
program's ``artifact_paths`` (``app.engine.strategy.registry``), so its bytes
are the sealed decision identity and a log level or payload tweak there would
invalidate every qualification receipt. Counting, logging, and receipting live
here, on the side of the boundary that already owns this run's operator
surface -- and in their own module rather than in ``bot_trade_strategy``,
which is already past a thousand lines (issue #1827).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    QUARANTINE_OUTCOME,
    DecisionOutcome,
    JsonValue,
)
from app.engine.strategy.signal_program import StageQuarantine
from app.services.bot_binding_repository import BrokerBotBinding
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# A systematically mis-shaped feed -- a program whose decision clock never
# matches the bucket width it is fed -- refuses *every* bar, so one warning
# per refusal would be one line per bar forever. Log the first of each reason
# immediately, then every _QUARANTINE_LOG_EVERY-th, always carrying the
# running count so the intervening refusals are represented rather than lost.
_QUARANTINE_LOG_EVERY = 50

# The one refusal reason for which the decision-clock pair means anything.
# ``SignalSession.advance`` owns these strings; its other two reasons
# (UNSETTLED_STAGE, NON_MONOTONIC_DECISION_CLOCK) refuse a bucket of exactly
# the right width, so reporting expected/observed alongside them is noise.
_TIMEFRAME_MISMATCH_REASON = "TIMEFRAME_MISMATCH"

class QuarantineReceiptSink(Protocol):
    """The ``SqliteDecisionReceipts.append`` shape this module writes through.

    Structural rather than the concrete class so a service module does not
    depend on the Clerk's storage implementation to record a refusal -- and
    so a test can drive the seam with a capture double instead of a database.
    Mirrors ``SqliteDecisionReceipts.append`` exactly; if that signature
    changes, this must follow it.
    """

    def append(
        self,
        *,
        outcome: DecisionOutcome,
        symbol: str | None,
        observed_at_ms: int,
        facts: Mapping[str, JsonValue],
        intent_id: str | None = ...,
        order_ref: str | None = ...,
    ) -> object: ...


def quarantine_bar_ref(binding: BrokerBotBinding, quarantine: StageQuarantine) -> str:
    """Name the refused *bucket*, not the source bar that triggered it.

    Parallel to ``bot_trade_strategy._decision_bar_ref``, but keyed on the
    bucket's own close: a quarantined bar never became an evaluation, so
    there is no ``decision_bar_close_ms`` to borrow.
    """
    return f"quarantined-bar:{binding.symbol}:{quarantine.bar_start_ms}-{quarantine.bar_end_ms}"


class QuarantineJournal:
    """Counts every refusal, logs a throttled sample, receipts the first.

    The receipt is written **once per distinct reason per runtime**, not once
    per refused bar. A mis-shaped decision clock refuses every bar, and
    decision receipts are pruned oldest-first against a per-instance cap
    (``MAX_DECISION_RECEIPTS_PER_STRATEGY``) -- a receipt per bar would evict
    the very decisions FR-016 crash replay must still see. One receipt per
    reason answers the operator's question ("this bot is consuming data and
    deciding nothing, and here is why") and is bounded by the three reasons
    ``SignalSession.advance`` can raise. The running count stays in the log.
    """

    def __init__(self, receipts: QuarantineReceiptSink | None = None) -> None:
        self._counts: dict[str, int] = {}
        self._receipts = receipts

    def record(
        self,
        quarantine: StageQuarantine,
        *,
        binding: BrokerBotBinding,
        expected_timeframe_ms: int,
    ) -> None:
        seen = self._counts.get(quarantine.reason, 0) + 1
        self._counts[quarantine.reason] = seen
        if seen == 1:
            self._append_receipt(quarantine, binding=binding, expected_timeframe_ms=expected_timeframe_ms)
        if seen > 1 and seen % _QUARANTINE_LOG_EVERY != 0:
            return
        logger.warning(
            "Trade bot quarantined a decision bar",
            extra=self._payload(
                quarantine, binding=binding, expected_timeframe_ms=expected_timeframe_ms, seen=seen
            ),
        )

    def _payload(
        self,
        quarantine: StageQuarantine,
        *,
        binding: BrokerBotBinding,
        expected_timeframe_ms: int,
        seen: int,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": "bot_decision_bar_quarantined",
            "strategy_instance_id": binding.strategy_instance_id,
            "strategy_key": binding.strategy_key,
            "symbol": binding.symbol,
            "reason": quarantine.reason,
            "status": quarantine.status.value,
            "bar_start_ms": quarantine.bar_start_ms,
            "bar_end_ms": quarantine.bar_end_ms,
            "quarantined_so_far": seen,
        }
        if quarantine.reason == _TIMEFRAME_MISMATCH_REASON:
            payload["expected_timeframe_ms"] = expected_timeframe_ms
            payload["observed_timeframe_ms"] = quarantine.observed_timeframe_ms
        return payload

    def _append_receipt(
        self,
        quarantine: StageQuarantine,
        *,
        binding: BrokerBotBinding,
        expected_timeframe_ms: int,
    ) -> None:
        if self._receipts is None:
            return
        facts: dict[str, JsonValue] = {
            "bar_ref": quarantine_bar_ref(binding, quarantine),
            "run_id": binding.run_id,
            "reason_code": quarantine.reason,
            # Survives tail compaction: this receipt is the explanation for a
            # run that produced no decisions, which is exactly the row an
            # operator reads after the fact.
            "retention_class": "protected_quarantine",
            "stage_status": quarantine.status.value,
            "bar_start_ms": quarantine.bar_start_ms,
            "bar_end_ms": quarantine.bar_end_ms,
            # Deliberately no `evaluation_id` / `decision_id`: a quarantined
            # bucket never became an evaluation. `run_replay_proof` excludes
            # this outcome before it reaches the identity requirement.
            "first_of_reason": True,
        }
        if quarantine.reason == _TIMEFRAME_MISMATCH_REASON:
            facts["expected_timeframe_ms"] = expected_timeframe_ms
            facts["observed_timeframe_ms"] = quarantine.observed_timeframe_ms
        self._receipts.append(
            outcome=QUARANTINE_OUTCOME,
            symbol=binding.symbol,
            observed_at_ms=now_ms_utc(),
            facts=facts,
        )
