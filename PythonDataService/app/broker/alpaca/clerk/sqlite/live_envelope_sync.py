"""One cadence observes account cash and judges effective account risk.

Explicit Apply policies live in Clerk custody. Until a Live account has one,
its historical arming envelope remains the compatibility policy. Paper has no
invented default. Observation publication, risk Apply, ENTER and guarded clear
share the repository writer fence. Unknown or breached evidence withdraws
entry permission; a standing hold only clears with fresh proof satisfying both
its original loss period/threshold and the current policy.

The arming refresh remains a separate compatibility collaborator until the
budget authority cutover retires it. It never replaces an explicitly applied
account risk policy or an immutable bot ExitTerms seal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SYNC_INTERVAL_S,
    AccountObservation,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.money import MoneyInputError, normalize_money
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, RiskRevisionConflict, append_risk_policy
from app.broker.alpaca.clerk.sqlite.arming_refresh import ArmingRefresh, InstanceSeals
from app.broker.alpaca.clerk.sqlite.day_pnl import (
    CASH_TRANSFER_ACTIVITY_FILTER,
    DayPnl,
    day_pnl_window_start_ms,
    observed_day_pnl,
    risk_evidence_ready,
    risk_fill_sequence,
)
from app.broker.alpaca.clerk.sqlite.facts import LossHoldClearBasis
from app.broker.alpaca.clerk.sqlite.lane_quiet import AccountQuietObservation, _custody_flat, observe_account_quiet
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection, SimulationEvidenceUnavailable
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold, resolve_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerAccountModeDisagreement, BrokerError, BrokerEvidenceUnavailable
from app.broker.contract.models import BrokerActivity
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]
EnvelopeSyncAction = Literal[
    "observed", "hold_raised", "hold_stands", "unknown", "read_failed", "mode_disagreed"
]

# What each verdict says to an operator, and how loudly. The three warnings
# are the ones that change what the account will accept.
_ACTION_MESSAGES: dict[EnvelopeSyncAction, str] = {
    "observed": "live envelope observed the account",
    "hold_raised": "live envelope raised the loss hold",
    "hold_stands": "live envelope loss hold still stands",
    "unknown": "live envelope cannot judge the account; every ENTER is refused",
    "read_failed": "live envelope could not read the account; the observation will age out",
    "mode_disagreed": "live envelope read a broker mode that no longer agrees; every ENTER is refused",
}
_WARNING_ACTIONS: frozenset[str] = frozenset(
    {"hold_raised", "read_failed", "unknown", "mode_disagreed"}
)


@dataclass(frozen=True)
class EnvelopeReading:
    """One tick's whole verdict on the account.

    ``day_pnl`` and ``loss_limit_usd`` are optional because either can be
    missing from a reading that is otherwise sound, and :attr:`breached`
    turns that into the third answer the loss rule needs — *unknown*, which
    is neither "breached" nor "safe".
    """

    observation: AccountObservation
    day_pnl: DayPnl | None
    loss_limit_usd: float | None
    # Whether this observation could read the account's arming inputs. Carried
    # on the reading, not asked of the sync afterwards, because it is one of
    # the four reasons the two figures above can be absent -- and the operator
    # surface that reports "unknown" has to name the right one.
    seal_readable: bool = True
    policy_revision: int | None = None
    daily_loss_exempt: bool = False
    fee_evidence_complete: bool = True

    @property
    def breached(self) -> bool | None:
        if self.day_pnl is None or not self.day_pnl.known or not math.isfinite(self.day_pnl.total_usd):
            return None
        if self.daily_loss_exempt:
            return False if self.fee_evidence_complete else None
        if self.loss_limit_usd is None:
            return None
        breached = loss_breached(day_pnl_usd=self.day_pnl.total_usd, loss_limit_usd=self.loss_limit_usd)
        return True if breached else False if self.fee_evidence_complete else None


@dataclass(frozen=True)
class AccountRiskSnapshot:
    policy: AccountRiskPolicy | None
    legacy_values: LiveEnvelopeValues | None
    observation: AccountObservation | None
    hold: LossHoldCause | None


def _breach_cause(reading: EnvelopeReading) -> LossHoldCause | None:
    """The cause to stamp on a raised hold, or ``None`` when nothing breached.

    Stamped from the reading that breached, never refreshed afterwards: the
    operator has to see the breach the hold was raised on, not the tick that
    happened to run last.
    """
    day_pnl, limit = reading.day_pnl, reading.loss_limit_usd
    last_equity = reading.observation.last_equity_usd
    if day_pnl is None or limit is None or last_equity is None or not reading.breached:
        return None
    return LossHoldCause(
        day_start_ms=day_pnl.day_start_ms,
        day_pnl_usd=day_pnl.total_usd,
        loss_limit_usd=limit,
        last_equity_usd=last_equity,
        observed_at_ms=reading.observation.observed_at_ms,
        policy_revision=reading.policy_revision,
    )


def _non_finite_risk_fields(
    *, cash: float, last_equity: float | None, equity: float | None
) -> tuple[str, ...]:
    """Which risk inputs the broker reported as NaN or infinity.

    ``adapter.opt_float`` is a bare ``float(value)``, so an Alpaca string like
    ``"NaN"`` arrives here as a genuine non-finite float. A NaN makes every
    loss comparison ``False``, which is indistinguishable from "nothing
    breached" — so a non-finite input is unjudgeable in exactly the way a
    missing ``last_equity`` is, and rides the same withdrawal.
    """
    return tuple(
        name
        for name, value in (
            ("cash", cash),
            ("last_equity", last_equity),
            ("equity", equity),
        )
        if value is not None and not math.isfinite(value)
    )


def _cash_flow_snapshots_match(
    before: list[BrokerActivity],
    after: list[BrokerActivity],
) -> bool:
    """Whether two reads describe the same economic transfer evidence.

    ``observed_at_ms`` is the local ingestion clock and can differ between
    otherwise identical broker rows, so it is deliberately excluded.
    """
    if len(before) != len(after):
        return False

    def by_id(activities: list[BrokerActivity]) -> dict[str, dict[str, Any]]:
        return {
            activity.activity_id: activity.model_dump(exclude={"observed_at_ms"})
            for activity in activities
        }

    before_by_id = by_id(before)
    return (
        len(before_by_id) == len(before)
        and len(by_id(after)) == len(after)
        and before_by_id == by_id(after)
    )


def _unknown_detail(reading: EnvelopeReading) -> dict[str, Any]:
    """Which fact left the account unjudgeable — the operator's whole diagnosis."""
    day_pnl = reading.day_pnl
    return {
        "sealed_envelope_readable": reading.seal_readable,
        "last_equity_known": reading.observation.last_equity_usd is not None,
        "cash_flows_known": None if day_pnl is None else day_pnl.cash_flows_known,
        "fee_evidence_complete": reading.fee_evidence_complete,
        "cash_flow_count": None if day_pnl is None else day_pnl.cash_flow_count,
    }


def _observed_detail(reading: EnvelopeReading) -> dict[str, Any]:
    """The healthy line, logged once when the account becomes judgeable and unbreached."""
    day_pnl = reading.day_pnl
    return {
        "cash_available_usd": reading.observation.cash_available_usd,
        "day_pnl_usd": None if day_pnl is None else day_pnl.total_usd,
        "current_equity_usd": None if day_pnl is None else day_pnl.current_equity_usd,
        "prior_close_equity_usd": (
            None if day_pnl is None else day_pnl.prior_close_equity_usd
        ),
        "net_cash_flow_usd": None if day_pnl is None else day_pnl.net_cash_flow_usd,
        "loss_limit_usd": reading.loss_limit_usd,
        "position_count": reading.observation.position_count,
    }


class LiveEnvelopeSync:
    """Drives the envelope's observation and its loss hold on one cadence.

    :meth:`tick` is the whole decision — one broker read, one verdict —
    which is what lets the promises be asserted without a running loop.
    """

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        read: BrokerReadPort,
        envelope: LiveEnvelopeGate,
        interval_s: float = ENVELOPE_SYNC_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
        max_ticks: int | None = None,
        arming_ledger: LiveArmingLedger | None = None,
        arming_gate: ArmingGate | None = None,
        instance_seals: InstanceSeals | None = None,
        simulation: SimulatedAccountProjection | None = None,
        custody_read: BrokerReadPort | None = None,
    ) -> None:
        if repo.account_id.startswith(("sim:", "shadow:")) and simulation is None:
            raise ValueError("Simulated custody requires its own cash and marked-risk projection")
        self._repo = repo
        self._simulation = simulation
        self._read = read
        # Shadow borrows only reference cash from Live; its obligations belong
        # to the guarded simulated custody port used by reconciliation.
        self._custody_read = read if custody_read is None else custody_read
        self.envelope = envelope
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_ticks = max_ticks
        self._arming_gate = arming_gate
        # The arming half of this cadence, or ``None`` where no ledger exists.
        # One collaborator, called once per tick: it owns the single ledger
        # read that feeds both the seal and the gate's snapshot.
        self._arming = (
            None
            if arming_ledger is None
            else ArmingRefresh(
                ledger=arming_ledger,
                gate=arming_gate,
                instance_seals=instance_seals,
                account_id=repo.account_id,
            )
        )
        # The previous tick's verdict, so an unchanged one is not re-logged.
        self._last_action: EnvelopeSyncAction | None = None
        self._account_mode_disagreed = False
        # The previous tick's non-finite risk fields, deduplicated the same way.
        self._last_non_finite: tuple[str, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # The broker account the last successful read described. Under shadow
        # that is the LIVE account, while ``self._repo.account_id`` is the
        # ``shadow:`` custody namespace. Money and P&L are projected from the
        # selected custody world. ``None`` until the first read returns.
        self._observed_account_id: str | None = None
        self._last_reading: EnvelopeReading | None = None
        self._observation_lock = asyncio.Lock()

    async def observe(self) -> EnvelopeReading:
        """Serialize account reads and publish their verdict under the custody fence.

        A failed mode check invalidates both published and cached evidence, so
        an overlapping risk Apply cannot revive it. Only a later successful
        matching read may restore permission. All callers share this boundary.
        """
        async with self._observation_lock:
            try:
                observation = await self._read_observation()
            except BrokerAccountModeDisagreement as exc:
                with self._repo._write_lock:
                    self._account_mode_disagreed = True
                    self.discard_observation()
                    if self._arming_gate is not None:
                        self._arming_gate.hold(LIVE_MODE_DISAGREEMENT, exc.detail or str(exc))
                raise
            with self._repo._write_lock:
                self._account_mode_disagreed = False
                if self._arming_gate is not None:
                    self._arming_gate.release()
                return self._evaluate_observation(observation, now_ms=self._repo.clock())

    async def _read_observation(self) -> AccountObservation:
        """Read broker facts without publishing new-exposure permission.

        The request-start timestamp conservatively bounds which fills the
        broker cash includes. A policy Apply during the network read is seen
        by the judgement after it returns; the earlier policy cannot publish
        an obsolete permission. Broker errors leave ordinary cadence data to
        age out; Configuration Apply discards failed evidence explicitly.
        """
        self.refresh_arming()
        # Stamped before the reads are issued, never when they return. The
        # stamp is the observation's claim about which fills its cash already
        # includes (``AccountObservation``), and a broker answer is only known
        # to be at least as recent as its request: a stamp taken on return
        # released the reservation of a fill recorded during the round trip
        # that the answer predated, and a second ENTER spent the same cash
        # (#2441).
        observed_at_ms = self._repo.clock()
        fill_sequence = risk_fill_sequence(self._repo)
        if self._simulation is not None:
            # Real positions and last_equity are never read into simulation.
            try:
                account = await self._read.get_account()
                returned_at_ms = self._repo.clock()
                self._observed_account_id = account.account_id
                with self._repo._write_lock:
                    return self._simulation.observe(reference_cash=account.cash,
                        observed_at_ms=observed_at_ms, now_ms=returned_at_ms)
            except MoneyInputError as exc:
                self.discard_observation()
                raise SimulationEvidenceUnavailable(str(exc)) from exc
            except BrokerError:
                self.discard_observation()
                raise
        current_start = day_pnl_window_start_ms(observed_at_ms)
        hold = self.risk_snapshot().hold
        start = min(current_start, self._held_period_start(hold)) if hold is not None else current_start
        try:
            before = await self._read.list_activities(after_ms=start, limit=100, activity_type=CASH_TRANSFER_ACTIVITY_FILTER)
            account = await self._read.get_account()
            after = await self._read.list_activities(after_ms=start, limit=100, activity_type=CASH_TRANSFER_ACTIVITY_FILTER)
        except BrokerEvidenceUnavailable:
            self.discard_observation()
            raise
        stable = day_pnl_window_start_ms(account.observed_at_ms) == current_start and _cash_flow_snapshots_match(before, after)
        self._observed_account_id = account.account_id
        return AccountObservation(
            observed_at_ms=observed_at_ms, broker_cash_usd=account.cash,
            cash_available_usd=account.cash, equity_usd=account.equity,
            last_equity_usd=account.last_equity, position_count=None,
            risk_fill_sequence=fill_sequence, risk_cash_flows=tuple(after),
            risk_cash_flow_evidence_complete=stable, risk_cash_flow_window_start_ms=start, risk_equity_window_start_ms=current_start,
        )

    def _evaluate_observation(self, observation: AccountObservation, *, now_ms: int) -> EnvelopeReading:
        """Rejudge current facts under the writer fence, using one effective policy."""
        policy = self._repo.account_risk_policy()
        revision = None if policy is None else policy.revision
        observation = replace(observation, risk_revision=revision)
        # Once explicit policy exists, the retired arming seal cannot override
        # account risk. Until then Live keeps its historical policy intact.
        synthetic = self._repo.account_id.startswith("sim:")
        readable = synthetic or policy is not None or (
            self.envelope.values is not None and not self._seal_unreadable()
        )
        unjudgeable = self._account_mode_disagreed or self._noted_non_finite(_non_finite_risk_fields(
            cash=observation.broker_cash_usd,
            last_equity=observation.last_equity_usd,
            equity=observation.equity_usd,
        )) or observation.equity_usd is None or observation.last_equity_usd is None or not readable or (self._simulation is not None and (
            observation.simulation_session_start_ms != et_day_window_ms(now_ms)[0]
            or (observation.simulation_marks_valid_until_ms is not None and now_ms > observation.simulation_marks_valid_until_ms)
        ))
        values = policy if policy is not None else (self.envelope.in_force if readable and self.envelope.values is not None else None)
        reading = EnvelopeReading(
            observation=observation, seal_readable=readable, policy_revision=revision, daily_loss_exempt=synthetic,
            fee_evidence_complete=risk_evidence_ready(self._repo, now_ms=now_ms),
            day_pnl=None if unjudgeable else observed_day_pnl(observation=observation, now_ms=now_ms),
            loss_limit_usd=None if unjudgeable or synthetic or observation.last_equity_usd is None else loss_limit_usd(
                values, last_equity_usd=observation.last_equity_usd,
            ),
        )
        if reading.day_pnl is not None and observation.risk_fill_sequence != risk_fill_sequence(self._repo):
            # New executions can make the observed equity/cash snapshot stale.
            # Refresh it before authorizing further exposure.
            reading = replace(reading, day_pnl=replace(reading.day_pnl, cash_flows_known=False))
        if reading.breached is False:
            self.envelope.publish(observation)
        else:
            self.envelope.withdraw()
        self._last_reading = reading
        return reading

    def risk_snapshot(self) -> AccountRiskSnapshot:
        with self._repo._write_lock:
            hold = self._repo.active_uncertainty(
                scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                strategy_instance_id=None,
            )
            return AccountRiskSnapshot(
                policy=self._repo.account_risk_policy(),
                legacy_values=self.envelope.in_force if self.envelope.values is not None else None,
                observation=self.envelope.fresh_observation(self._repo.clock()),
                hold=None if hold is None else LossHoldCause.from_mapping(json.loads(hold["facts_json"])["cause_facts"]),
            )

    def discard_observation(self) -> None:
        with self._repo._write_lock:
            self.envelope.withdraw()
            self._last_reading = None

    def apply_risk_policy(self, policy: AccountRiskPolicy, *, expected_revision: int) -> AccountRiskSnapshot:
        """Commit and judge one Apply before another ENTER acquires the writer.

        No network call occurs under the fence. Missing/stale evidence keeps
        entries withdrawn, while the receipt honestly reports effective limits.
        """
        with self._repo._write_lock:
            current = self._repo.account_risk_policy()
            if (0 if current is None else current.revision) != expected_revision:
                raise RiskRevisionConflict("Risk limits changed since review. Reload and review again.")
            last = self._last_reading
            self.envelope.withdraw()
            append_risk_policy(self._repo, policy=policy, expected_revision=expected_revision)
            if last is None or not 0 <= self._repo.clock() - last.observation.observed_at_ms <= self.envelope.observation_max_age_ms:
                return self.risk_snapshot()
            reading = self._evaluate_observation(last.observation, now_ms=self._repo.clock())
            self._raise_loss_hold(reading)
            return self.risk_snapshot()

    def _raise_loss_hold(self, reading: EnvelopeReading) -> str | None:
        if self._hold_stands():
            return None
        cause = _breach_cause(reading)
        if cause is None:
            return None
        return raise_account_hold(
            self._repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            evidence_refs=[f"day-pnl:{cause.day_start_ms}"], cause_facts=cause.to_mapping(),
        )

    def _held_period_start(self, cause: LossHoldCause) -> int:
        # Historical real holds recorded ET midnight with a prior-close
        # baseline. Read the full baseline horizon without rewriting cause.
        return cause.day_start_ms if self._simulation is not None else day_pnl_window_start_ms(cause.observed_at_ms)

    async def observe_loss_clearance(self) -> tuple[EnvelopeReading, AccountQuietObservation | None]:
        reading = await self.observe()
        snapshot = self.risk_snapshot()
        needs_reset = snapshot.hold is not None and reading.day_pnl is not None and reading.day_pnl.day_start_ms > self._held_period_start(snapshot.hold)
        quiet = await observe_account_quiet(self._repo, self._custody_read) if needs_reset else None
        return reading, quiet

    def clear_observed_loss_hold(self, reading: EnvelopeReading, *, quiet: AccountQuietObservation | None = None) -> tuple[str, str]:
        """Prove current policy and the retained breach together, then resolve.

        Same-session proof retains the original loss threshold. In a later
        session the explicit clear may reset a completed, fully resolved loss
        period only with fresh double-read account-quiet proof. The new session
        must satisfy the original dollar threshold and current limits too.
        """
        with self._repo._write_lock:
            current = self._repo.account_risk_policy()
            revision = None if current is None else current.revision
            if revision != reading.policy_revision or self.envelope.fresh_observation(self._repo.clock()) is None:
                return "unknown", "The risk policy or observation changed. Refresh and clear again."
            current_reading = self._evaluate_observation(reading.observation, now_ms=self._repo.clock())
            if current_reading.breached is None:
                return "unknown", "Current loss evidence cannot be judged. The hold stands."
            if current_reading.breached:
                return "held", "The current effective loss limit remains breached. The hold stands."
            hold = self._repo.active_uncertainty(
                scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                strategy_instance_id=None,
            )
            if hold is None:
                return "no_hold", "The hold was already released."
            try:
                cause = LossHoldCause.from_mapping(json.loads(hold["facts_json"])["cause_facts"])
            except (ValueError, TypeError, KeyError):
                return "unknown", "The original loss-hold evidence is incomplete. The hold stands."
            retained = observed_day_pnl(observation=reading.observation,
                now_ms=self._repo.clock(), retained_start_ms=self._held_period_start(cause),
                retained_equity_usd=cause.last_equity_usd)
            if not retained.known or not math.isfinite(retained.total_usd):
                return "unknown", "The original loss period cannot be judged. The hold stands."
            session_reset = current_reading.day_pnl.day_start_ms > self._held_period_start(cause)
            judged_pnl = retained.total_usd
            if session_reset:
                if quiet is None or not 0 <= self._repo.clock() - quiet.observed_at_ms <= self.envelope.observation_max_age_ms:
                    return "unknown", "Fresh proof that the previous session's obligations are resolved is required. The hold stands."
                if not (quiet.broker_work_ended and quiet.account_flat and quiet.intents_resolved) or self._repo.reconcilable_effect_operations() or not _custody_flat(self._repo):
                    return "held", "The previous loss period still has open or unresolved obligations. Resolve them before clearing."
                judged_pnl = current_reading.day_pnl.total_usd
            if loss_breached(day_pnl_usd=judged_pnl, loss_limit_usd=cause.loss_limit_usd):
                return "held", "The loss still breaches the limit recorded when this hold began. The hold stands."
            basis = LossHoldClearBasis(
                original_session_start_ms=cause.day_start_ms,
                original_policy_revision=cause.policy_revision,
                original_baseline_usd=cause.last_equity_usd,
                original_loss_limit_usd=cause.loss_limit_usd,
                current_session_start_ms=current_reading.day_pnl.day_start_ms,
                current_policy_revision=revision,
                current_loss_limit_usd=current_reading.loss_limit_usd,
                current_day_pnl_usd=current_reading.day_pnl.total_usd,
                observed_at_ms=current_reading.observation.observed_at_ms,
                session_reset=session_reset,
            )
            released = resolve_account_hold(
                self._repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                summary_code="LIVE_ENVELOPE_LOSS_HOLD_CLEARED", loss_hold_clear_basis=basis,
            )
            detail = "Loss hold cleared for the new session after proving prior obligations resolved and both loss limits safe." if session_reset else "Both the current policy and the original loss limit are safe. Loss hold cleared."
            return ("cleared", detail) if released else ("no_hold", "The hold was already released.")

    def _seal_unreadable(self) -> bool:
        """Whether this observation could not read the account's arming inputs.

        Such an account cannot be judged. ``LiveEnvelopeGate.in_force`` would
        fall back to the configured values, and here that fallback is a
        *relaxation*: an operator could loosen ``ALPACA_LIVE_LOSS_*``, corrupt
        or delete the arming ledger, and clear a standing hold against the
        looser limit -- the very drift this seal exists to stop. So it rides the
        same withdrawal an unknown day P&L does, and every ENTER refuses
        ``LIVE_ENVELOPE_UNOBSERVED`` until the inputs read again.

        Pricing an extended-hours leg is the deliberate opposite
        (``sqlite/runtime.py::SqliteAlpacaClerkFacade.program_leg_policy``): it
        falls back and never refuses, because an EXIT leaves the account.
        """
        return self._repo.budget_authority_version() < 2 and self._arming is not None and self._arming.inputs_unreadable

    async def refresh_private_starting_cash(self, amount: Decimal) -> None:
        """Refresh transient reviewed cash; durable consent always owns the seed."""
        if not self._repo.account_id.startswith("sim:") or self._simulation is None:
            raise ValueError("Only a private Dry Run accepts starting cash")
        with self._repo._write_lock:
            self.envelope.withdraw()
            if not self._repo._conn.execute("SELECT 1 FROM deployment_budgets LIMIT 1").fetchone():
                self._simulation.initial_cash = normalize_money(amount)
        await self.tick()

    def refresh_arming(self) -> None:
        """Run the arming half of this observation, and seal the envelope from what it read.

        Called from :meth:`observe`, ahead of the broker read, so every caller
        of that method -- the cadence and the guarded operator clear alike --
        judges against the seal the ledger holds right now.
        """
        if self._repo.budget_authority_version() >= 2:
            self.envelope.sealed = None
            return
        if self._arming is None or self.envelope.values is None:
            return
        self._assign_sealed(self._arming.refresh(self._repo.clock(), self.envelope.values))

    def _assign_sealed(self, sealed: LiveEnvelopeValues | None) -> None:
        """Assign the sealed envelope, logging each transition once (slice 6 R10).

        Called only from :meth:`refresh_arming`, past its no-ledger return,
        so ``self._arming`` is never ``None`` here.
        """
        if sealed == self.envelope.sealed:
            return
        self.envelope.sealed = sealed
        emit = logger.warning if self.envelope.agreement == "disagreed" else logger.info
        emit(
            "live envelope sealed by an arming record"
            if sealed is not None
            else "live envelope is no longer sealed by any arming record",
            extra={
                "action": "live_envelope_sealed" if sealed is not None else "live_envelope_unsealed",
                # Two ids, always: under shadow the custody id this Clerk
                # writes against is ``shadow:<id>`` while the ledger the
                # envelope was sealed from is rooted at the live ``<id>``.
                "account_id": self._repo.account_id,
                "live_account_id": self._arming.live_account_id,
                "agreement": self.envelope.agreement,
            },
        )

    @property
    def account_mode_disagreed(self) -> bool:
        """Latest proven mode disagreement; a failed read cannot clear it."""
        return self._account_mode_disagreed

    async def tick(self) -> EnvelopeSyncAction:
        """Observe once, and act on the verdict.

        The hold is checked before the breach so a standing hold is never
        re-raised: its cause is the breach it was raised on, and refreshing
        it with a later tick's numbers would churn the control revision and
        overwrite the evidence the operator is reading.
        """
        try:
            reading = await self.observe()
        except BrokerAccountModeDisagreement as exc:
            # The common observation boundary already withdrew every copy
            # of the invalid evidence under the same fence as policy Apply.
            return self._acted("mode_disagreed", {"why": exc.detail or str(exc)})
        except BrokerEvidenceUnavailable as exc:
            return self._acted("unknown", {"why": str(exc)})
        except BrokerError as exc:
            # Not a verdict on the mode either way: a failed read leaves a
            # standing disagreement standing, and the observation ages out.
            return self._acted("read_failed", {"why": str(exc)})
        with self._repo._write_lock:
            # An Apply can finish during the broker read. Rejudge inside the
            # same fence as ENTER before raising the durable cause.
            reading = self._evaluate_observation(reading.observation, now_ms=self._repo.clock())
            if self._hold_stands():
                return self._acted("hold_stands", {} if reading.breached is not None else {"why": "the account is also unjudgeable", **_unknown_detail(reading)})
            if reading.breached is None:
                return self._acted("unknown", _unknown_detail(reading))
            outcome = self._raise_loss_hold(reading)
            if outcome is None:
                return self._acted("observed", _observed_detail(reading))
            return self._acted("hold_raised", {"outcome": outcome})

    def _noted_non_finite(self, fields: tuple[str, ...]) -> bool:
        """Log a changed non-finite verdict, and say whether one stands.

        Deduplicated like ``_acted``: at a 15 s cadence a persistently bad
        feed would write four identical lines a minute and bury the tick that
        changed what the account accepts. The line is separate from the
        ``unknown`` verdict's because it names a fault in the broker's
        numbers, not a fact the Clerk cannot know.
        """
        if fields and fields != self._last_non_finite:
            logger.warning(
                "live envelope read a non-finite risk figure; the account cannot be judged",
                extra={
                    "action": "live_envelope_read_non_finite",
                    "fields": list(fields),
                    "account_id": self._repo.account_id,
                    "observed_account_id": self._observed_account_id,
                },
            )
        self._last_non_finite = fields
        return bool(fields)

    def _hold_stands(self) -> bool:
        return (
            self._repo.active_uncertainty(
                scope="ACCOUNT_CLERK",
                reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                strategy_instance_id=None,
            )
            is not None
        )

    def _acted(self, action: EnvelopeSyncAction, detail: dict[str, Any]) -> EnvelopeSyncAction:
        """Record the verdict, logging only when it differs from the last one.

        At a 15 s cadence an unchanged verdict — a persisting outage as much
        as a healthy account — would write four lines a minute and bury the
        transition that actually changed what the account accepts.
        """
        if action != self._last_action:
            emit = logger.warning if action in _WARNING_ACTIONS else logger.info
            emit(
                _ACTION_MESSAGES[action],
                extra={
                    "action": f"live_envelope_{action}",
                    # Both, always: the custody account the hold is written
                    # against, and the broker account the cash, equity and
                    # positions were read from. Under shadow they differ, and
                    # correlating a figure to the wrong ledger is a wasted
                    # incident.
                    "account_id": self._repo.account_id,
                    "observed_account_id": self._observed_account_id,
                    **detail,
                },
            )
        self._last_action = action
        return action

    async def run(self) -> None:
        ticks = 0
        while self._max_ticks is None or ticks < self._max_ticks:
            try:
                await self.tick()
            except Exception:
                # A failed tick must not kill the loop: an unattended dead
                # sync is how the envelope goes blind to a losing day.
                logger.exception(
                    "live envelope sync tick failed",
                    extra={"action": "live_envelope_sync_failed"},
                )
            ticks += 1
            await self._sleep(self._interval_s)

    def start(self) -> None:
        """Begin the loop, unless this sync has already been stopped.

        ``stop()`` closes the projection reader the constructor opened, and a
        reader is never reopened -- so a restarted loop would tick against a
        closed connection and fail every 15 s. Refusing here names the
        programming error instead of burying it in the swallowed-tick log.
        """
        if self._stopped:
            raise RuntimeError("LiveEnvelopeSync is terminal after stop()")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="alpaca-live-envelope-sync")

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


__all__ = ["EnvelopeReading", "EnvelopeSyncAction", "InstanceSeals", "LiveEnvelopeSync"]
