"""Shadow-vs-paper-twin reconciliation and the shadow gate (ADR 0059 D2).

A shadow session counts only when (a) the sweep reconciled cleanly for the
whole day, (b) the instance's run covered its whole decision session, and
(c) the instance's synthesized trades reconcile against its paper twin — the
same configured signal, plan and size sealed to the paper account, run over
the same day. Shadow proves the live plumbing; it does not prove execution
quality, so the twin comparison gates on *decisions* and only reports prices.

Math Provenance Contract (reconcile_twin_day)
--------------------------------------------
Formula: order each side's fills by ``(filled_at_ms, order_ref)`` and pair
them by index. The first failing rule classifies a pair: a fill with no
partner, or partners on different symbols, is ``DECISION_MISMATCH``;
different sides, ``DIRECTION_MISMATCH``; different quantities,
``QUANTITY_MISMATCH``; otherwise ``|shadow.price - twin.price| > atol`` is
``FILL_PRICE_DRIFT``. The gating set is ``{DECISION_MISMATCH,
DIRECTION_MISMATCH, QUANTITY_MISMATCH}``; ``passed`` iff no gating divergence.
``atol = $0.01``, the taxonomy's ``fill_price_atol`` default.
Reference: ``.claude/rules/numerical-rigor.md`` § "Trade-level reconciliation
taxonomy" (the ``DivergenceCategory`` enum); ADR 0059 D2 — "synthesized fills
are optimistic by construction", so price drift is reported, never gated.
Canonical implementation: this file.
Validated against: ``tests/services/test_alpaca_shadow_reconciliation.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from app.broker.alpaca.clerk.account_authority import is_shadow_account_id
from app.broker.alpaca.clerk.shadow_sessions import ShadowDayState, ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.models import RunResource
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.research.parity.qc_reconciler import DivergenceCategory
from app.schemas.signal_program_seal import SealedBotProgram
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import declared_session_bounds
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms

FILL_PRICE_ATOL = Decimal("0.01")
GATING_CATEGORIES: frozenset[DivergenceCategory] = frozenset(
    {
        DivergenceCategory.DECISION_MISMATCH,
        DivergenceCategory.DIRECTION_MISMATCH,
        DivergenceCategory.QUANTITY_MISMATCH,
    }
)
SessionState = Literal[
    "counted",
    "sweep_not_clean",
    "sweep_opened_late",
    "run_not_covering",
    "twin_diverged",
    "not_evaluable",
]


@dataclass(frozen=True)
class TwinFill:
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    filled_at_ms: int
    order_ref: str


@dataclass(frozen=True)
class TwinDivergence:
    category: DivergenceCategory
    index: int
    detail: str


@dataclass(frozen=True)
class TwinDayReconciliation:
    session_open_ms: int
    strategy_instance_id: str
    twin_strategy_instance_id: str
    shadow_fills: tuple[TwinFill, ...]
    twin_fills: tuple[TwinFill, ...]
    divergences: tuple[TwinDivergence, ...]
    max_fill_price_drift: Decimal | None
    fill_price_atol: Decimal

    @property
    def gating(self) -> tuple[TwinDivergence, ...]:
        return tuple(d for d in self.divergences if d.category in GATING_CATEGORIES)

    @property
    def passed(self) -> bool:
        return not self.gating

    def report_sha256(self) -> str:
        """A content hash of the whole comparison — what the receipt names."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ordered(fills: Sequence[TwinFill]) -> tuple[TwinFill, ...]:
    return tuple(sorted(fills, key=lambda fill: (fill.filled_at_ms, fill.order_ref)))


def reconcile_twin_day(
    *,
    session_open_ms: int,
    strategy_instance_id: str,
    twin_strategy_instance_id: str,
    shadow_fills: Sequence[TwinFill],
    twin_fills: Sequence[TwinFill],
    fill_price_atol: Decimal = FILL_PRICE_ATOL,
) -> TwinDayReconciliation:
    """Pair one ET day's shadow fills against the paper twin's under the taxonomy."""
    shadow, twin = _ordered(shadow_fills), _ordered(twin_fills)
    divergences: list[TwinDivergence] = []
    drifts: list[Decimal] = []
    for index in range(max(len(shadow), len(twin))):
        left = shadow[index] if index < len(shadow) else None
        right = twin[index] if index < len(twin) else None
        if left is None or right is None:
            side = "twin" if left is None else "shadow"
            divergences.append(
                TwinDivergence(
                    DivergenceCategory.DECISION_MISMATCH,
                    index,
                    f"only the {side} side has fill #{index}",
                )
            )
            continue
        if left.symbol != right.symbol:
            divergences.append(
                TwinDivergence(
                    DivergenceCategory.DECISION_MISMATCH,
                    index,
                    f"{left.symbol} vs {right.symbol}",
                )
            )
            continue
        if left.side != right.side:
            divergences.append(
                TwinDivergence(
                    DivergenceCategory.DIRECTION_MISMATCH,
                    index,
                    f"{left.side} vs {right.side}",
                )
            )
            continue
        if left.quantity != right.quantity:
            divergences.append(
                TwinDivergence(
                    DivergenceCategory.QUANTITY_MISMATCH,
                    index,
                    f"{left.quantity} vs {right.quantity}",
                )
            )
            continue
        drift = abs(left.fill_price - right.fill_price)
        drifts.append(drift)
        if drift > fill_price_atol:
            divergences.append(
                TwinDivergence(
                    DivergenceCategory.FILL_PRICE_DRIFT,
                    index,
                    f"|{left.fill_price} - {right.fill_price}| = {drift} > {fill_price_atol}",
                )
            )
    return TwinDayReconciliation(
        session_open_ms=session_open_ms,
        strategy_instance_id=strategy_instance_id,
        twin_strategy_instance_id=twin_strategy_instance_id,
        shadow_fills=shadow,
        twin_fills=twin,
        divergences=tuple(divergences),
        max_fill_price_drift=max(drifts) if drifts else None,
        fill_price_atol=fill_price_atol,
    )


class FillSource(Protocol):
    """Where one authority's fills and runs are read from (a read-only projection in production)."""

    def fills_between(
        self, *, strategy_instance_id: str, from_ms: int, to_ms: int
    ) -> tuple[TwinFill, ...]:
        """One instance's fills in the half-open window ``[from_ms, to_ms)``.

        Half-open is the convention the SQLite projection already implements
        (``economic_filled_at_ms < to_ms``), so it is the one every
        implementation — the production adapter and any double — must hold to.
        """
        ...

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        """Every run this authority recorded for one instance, oldest first."""
        ...


class EconomicFillSource:
    """``FillSource`` over ``SqliteEconomicProjectionReader`` (``mode=ro``; never the lease)."""

    def __init__(self, reader: SqliteEconomicProjectionReader) -> None:
        self._reader = reader

    @classmethod
    def from_database_path(cls, db_path: Path) -> EconomicFillSource:
        return cls(SqliteEconomicProjectionReader.from_database_path(db_path))

    def fills_between(
        self, *, strategy_instance_id: str, from_ms: int, to_ms: int
    ) -> tuple[TwinFill, ...]:
        """This instance's fills in ``[from_ms, to_ms)`` — the protocol's half-open window.

        ``account_fill_window`` is account-wide and identifies every fill by its
        **custody subject**, so the instance filter is on ``bot_subject_id``, not
        on the bare instance id (a manual fill has no instance id at all).
        """
        subject_id = bot_subject_id(strategy_instance_id)
        records = self._reader.account_fill_window(from_ms=from_ms, to_ms=to_ms)
        return tuple(
            TwinFill(
                symbol=record.symbol,
                side=str(record.side),
                quantity=Decimal(str(record.quantity)),
                fill_price=Decimal(str(record.fill_price)),
                filled_at_ms=record.filled_at_ms,
                order_ref=record.order_ref,
            )
            for record in records
            if record.sid == subject_id
        )

    def runs_for_strategy(self, strategy_instance_id: str) -> tuple[RunResource, ...]:
        return self._reader.runs_for_strategy(strategy_instance_id)

    def close(self) -> None:
        self._reader.close()


def read_twin_fills(
    source: FillSource, *, strategy_instance_id: str, session_open_ms: int
) -> tuple[TwinFill, ...]:
    """The ET calendar day's fills for one instance (a shadow extended fill can land after the close).

    ``[et_midnight_ms(day), et_day_end_ms(day))`` is the whole ET day under the
    protocol's half-open convention: ``et_day_end_ms`` is already the next day's
    ET midnight, so the day's final millisecond is in and the next day's first
    is out.
    """
    day = et_date_at_ms(session_open_ms)
    return source.fills_between(
        strategy_instance_id=strategy_instance_id,
        from_ms=et_midnight_ms(day),
        to_ms=et_day_end_ms(day),
    )


class ShadowTwinMismatch(ValueError):
    """The named twin is not this instance's paper twin."""

    reason_code = "SHADOW_TWIN_MISMATCH"


def twins_agree(shadow: SealedBotProgram, twin: SealedBotProgram) -> str | None:
    """``None`` when ``twin`` is ``shadow``'s paper twin; otherwise the first disagreement.

    Identity is the configured signal, the action plan, the size and the
    carryover policy — never the instance id, the account, or the outer seal
    hash (both of those legitimately differ between the two worlds).
    """
    if not is_shadow_account_id(shadow.sealed_account_id):
        return "the shadow side is not sealed to a shadow: account"
    if is_shadow_account_id(twin.sealed_account_id):
        return "the twin is sealed to a shadow: account, not the paper account"
    if shadow.mode != "trade" or twin.mode != "trade":
        return "both twins must be trade-mode bindings"
    if shadow.configured_signal_hash != twin.configured_signal_hash:
        return "the twins do not share a configured signal"
    if shadow.action_plan != twin.action_plan:
        return "the twins do not share an action plan"
    if shadow.quantity != twin.quantity:
        return "the twins do not share a size"
    if shadow.carryover_policy != twin.carryover_policy:
        return "the twins do not share a carryover policy"
    return None


@dataclass(frozen=True)
class ShadowSessionVerdict:
    session_open_ms: int
    state: SessionState
    detail: str
    shadow_run_id: str | None
    reconciliation: TwinDayReconciliation | None


@dataclass(frozen=True)
class ShadowGateEvaluation:
    live_account_id: str
    strategy_instance_id: str
    twin_account_id: str
    twin_strategy_instance_id: str
    configured_signal_hash: str
    required_sessions: int
    sessions: tuple[ShadowSessionVerdict, ...]

    @property
    def counted(self) -> tuple[ShadowSessionVerdict, ...]:
        return tuple(verdict for verdict in self.sessions if verdict.state == "counted")

    @property
    def satisfied(self) -> bool:
        return len(self.counted) >= self.required_sessions


def _decision_span(session: RunDecisionSession, day: date) -> tuple[int, int]:
    if session.kind == "rth":
        return session_open_ms_utc(day), session_close_ms_utc(day)
    bounds = declared_session_bounds(day, session.window)
    assert bounds is not None  # ``day`` comes from expected_sessions
    return bounds.open_ms, bounds.close_ms


def _covering_run(
    runs: Sequence[RunResource], *, open_ms: int, close_ms: int
) -> RunResource | None:
    return next(
        (
            run
            for run in runs
            if run.started_at_ms <= open_ms
            and (run.stopped_at_ms is None or run.stopped_at_ms >= close_ms)
        ),
        None,
    )


def evaluate_shadow_gate(
    *,
    live_account_id: str,
    shadow_binding: BrokerBotBinding,
    twin_binding: BrokerBotBinding,
    twin_account_id: str,
    required_sessions: int,
    session_ledger: ShadowSessionLedger,
    shadow_source: FillSource,
    twin_source: FillSource,
    window: ExtendedHoursWindow | None,
    now_ms: int,
) -> ShadowGateEvaluation:
    """Judge every trading day since the shadow instance first ran (ADR 0059 D2)."""
    if shadow_binding.sealed_program is None or twin_binding.sealed_program is None:
        raise ShadowTwinMismatch("both bindings must carry their sealed program")
    disagreement = twins_agree(shadow_binding.sealed_program, twin_binding.sealed_program)
    if disagreement is not None:
        raise ShadowTwinMismatch(disagreement)
    runs = shadow_source.runs_for_strategy(shadow_binding.strategy_instance_id)
    session = RunDecisionSession.resolve(use_rth=shadow_binding.use_rth, window=window)
    verdicts: list[ShadowSessionVerdict] = []
    if runs and session is not None:
        first_day = et_date_at_ms(min(run.started_at_ms for run in runs))
        for day in expected_sessions(first_day, et_date_at_ms(now_ms)):
            open_ms, close_ms = _decision_span(session, day)
            if close_ms > now_ms:
                continue
            verdicts.append(
                _judge_day(
                    day,
                    open_ms=open_ms,
                    close_ms=close_ms,
                    runs=runs,
                    session_ledger=session_ledger,
                    shadow_source=shadow_source,
                    twin_source=twin_source,
                    strategy_instance_id=shadow_binding.strategy_instance_id,
                    twin_strategy_instance_id=twin_binding.strategy_instance_id,
                )
            )
    elif runs:
        verdicts.append(
            ShadowSessionVerdict(
                session_open_ms=session_open_ms_utc(et_date_at_ms(runs[0].started_at_ms)),
                state="not_evaluable",
                detail="an extended-session binding has no declared window to judge against",
                shadow_run_id=None,
                reconciliation=None,
            )
        )
    return ShadowGateEvaluation(
        live_account_id=live_account_id,
        strategy_instance_id=shadow_binding.strategy_instance_id,
        twin_account_id=twin_account_id,
        twin_strategy_instance_id=twin_binding.strategy_instance_id,
        configured_signal_hash=shadow_binding.sealed_program.configured_signal_hash,
        required_sessions=required_sessions,
        sessions=tuple(verdicts),
    )


def _sweep_failure(state: ShadowDayState) -> str:
    if state.non_clean_verdicts:
        return f"non-clean sweep verdicts: {', '.join(state.non_clean_verdicts)}"
    if state.opened_at_ms is None:
        return "the sweep never opened the day"
    return "the sweep did not close the day clean"


def _judge_day(
    day: date,
    *,
    open_ms: int,
    close_ms: int,
    runs: Sequence[RunResource],
    session_ledger: ShadowSessionLedger,
    shadow_source: FillSource,
    twin_source: FillSource,
    strategy_instance_id: str,
    twin_strategy_instance_id: str,
) -> ShadowSessionVerdict:
    calendar_open_ms = session_open_ms_utc(day)
    state = session_ledger.day_state(calendar_open_ms)
    if not state.complete:
        return ShadowSessionVerdict(
            calendar_open_ms, "sweep_not_clean", _sweep_failure(state), None, None
        )
    if state.opened_at_ms is not None and state.opened_at_ms > open_ms:
        return ShadowSessionVerdict(
            calendar_open_ms,
            "sweep_opened_late",
            "the sweep's first pass came after the decision session opened",
            None,
            None,
        )
    run = _covering_run(runs, open_ms=open_ms, close_ms=close_ms)
    if run is None:
        return ShadowSessionVerdict(
            calendar_open_ms,
            "run_not_covering",
            "no run of this instance spanned the whole decision session",
            None,
            None,
        )
    try:
        shadow_fills = read_twin_fills(
            shadow_source,
            strategy_instance_id=strategy_instance_id,
            session_open_ms=calendar_open_ms,
        )
        twin_fills = read_twin_fills(
            twin_source,
            strategy_instance_id=twin_strategy_instance_id,
            session_open_ms=calendar_open_ms,
        )
    except EconomicProjectionUnavailable as exc:
        # Not a swallowed error: a window an authority will not vouch for is a
        # day that cannot be judged, so it becomes a typed state carrying the
        # reader's own sentence, and it does not count. One such day must not
        # destroy every other day's verdict — a fail-closed account-wide read
        # (an external fill, an unresolved coverage conflict) makes *every* day
        # unreadable, and the gate has to say so honestly rather than raise.
        return ShadowSessionVerdict(
            session_open_ms=calendar_open_ms,
            state="not_evaluable",
            detail=str(exc),
            shadow_run_id=run.run_id,
            reconciliation=None,
        )
    reconciliation = reconcile_twin_day(
        session_open_ms=calendar_open_ms,
        strategy_instance_id=strategy_instance_id,
        twin_strategy_instance_id=twin_strategy_instance_id,
        shadow_fills=shadow_fills,
        twin_fills=twin_fills,
    )
    if not reconciliation.passed:
        detail = "; ".join(f"{d.category}: {d.detail}" for d in reconciliation.gating)
        return ShadowSessionVerdict(
            calendar_open_ms, "twin_diverged", detail, run.run_id, reconciliation
        )
    return ShadowSessionVerdict(calendar_open_ms, "counted", "", run.run_id, reconciliation)


__all__ = [
    "FILL_PRICE_ATOL",
    "GATING_CATEGORIES",
    "EconomicFillSource",
    "FillSource",
    "ShadowGateEvaluation",
    "ShadowSessionVerdict",
    "ShadowTwinMismatch",
    "TwinDayReconciliation",
    "TwinDivergence",
    "TwinFill",
    "evaluate_shadow_gate",
    "read_twin_fills",
    "reconcile_twin_day",
    "twins_agree",
]
