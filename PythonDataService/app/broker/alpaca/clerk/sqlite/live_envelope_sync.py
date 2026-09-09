"""Independent fixed-cadence lifecycle for the live envelope (ADR 0059 D4).

Modelled on ``StreamHealthHoldSync``: one background tap produces the
account observation and the loss hold; ``accept_enter`` consumes them and
never contacts the broker. Decoupled from the reconciliation pass, whose
backoff reaches 300 s on failure — exactly when a loss hold matters most.

The sync raises the loss hold and never releases it: only the guarded
operator action does (plan R6, R12).

Three facts leave the account *unjudgeable* rather than merely unlucky: a
broker snapshot with no ``last_equity`` has no loss limit to judge against
(plan R3), an external order observed today makes the day's P&L unknowable
(plan R5), and a non-finite cash, equity or unrealized figure makes every
loss comparison meaningless. None may be read as "nothing breached", so an
unjudgeable tick withdraws the gate's observation and every ENTER refuses
``LIVE_ENVELOPE_UNOBSERVED`` until a judgeable one arrives.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.live_arming import (
    LIVE_MODE_DISAGREEMENT,
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingInvalid,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SYNC_INTERVAL_S,
    AccountObservation,
    LiveEnvelopeGate,
    LiveEnvelopeValues,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import DayPnl, day_pnl_at
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    LossHoldCause,
)
from app.broker.contract.errors import BrokerAccountModeDisagreement, BrokerError
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]
EnvelopeSyncAction = Literal[
    "observed", "hold_raised", "hold_stands", "unknown", "read_failed", "mode_disagreed"
]

# The runner's sealed bindings, read per tick: ``strategy_instance_id`` -> the
# instance's current sealed-program hash. Injected as a callable so the clerk
# layer stays free of bot-registration imports (the ``roster_symbols`` pattern).
type InstanceSeals = Callable[[], Mapping[str, str]]

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

    @property
    def breached(self) -> bool | None:
        if self.day_pnl is None or not self.day_pnl.known or self.loss_limit_usd is None:
            return None
        return loss_breached(day_pnl_usd=self.day_pnl.total_usd, loss_limit_usd=self.loss_limit_usd)


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
    )


def _non_finite_risk_fields(
    *, cash: float, last_equity: float | None, unrealized_pl: float
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
            ("unrealized_pl", unrealized_pl),
        )
        if value is not None and not math.isfinite(value)
    )


def _unknown_detail(reading: EnvelopeReading) -> dict[str, Any]:
    """Which fact left the account unjudgeable — the operator's whole diagnosis."""
    day_pnl = reading.day_pnl
    return {
        "last_equity_known": reading.observation.last_equity_usd is not None,
        "external_orders_today": None if day_pnl is None else day_pnl.external_orders_today,
        "execution_coverage": None if day_pnl is None else day_pnl.execution_coverage,
        "fee_fidelity": None if day_pnl is None else day_pnl.fee_fidelity,
    }


def _observed_detail(reading: EnvelopeReading) -> dict[str, Any]:
    """The healthy line, logged once when the account becomes judgeable and unbreached."""
    day_pnl = reading.day_pnl
    return {
        "cash_available_usd": reading.observation.cash_available_usd,
        "day_pnl_usd": None if day_pnl is None else day_pnl.total_usd,
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
    ) -> None:
        self._repo = repo
        self._read = read
        self.envelope = envelope
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_ticks = max_ticks
        self._arming_ledger = arming_ledger
        self._arming_gate = arming_gate
        self._instance_seals = instance_seals
        # The instances the previous tick judged armed, so an instance leaving
        # that set is warned about exactly once (ADR 0059 D8 as design R10).
        self._previously_armed: frozenset[str] = frozenset()
        # Set by a read the broker answered in the wrong mode (design R2); the
        # gate stays invalid under LIVE_MODE_DISAGREEMENT until a read agrees.
        self._mode_disagreed = False
        # Whether the last refresh found the ledger unreadable, so the error is
        # logged once per transition rather than four times a minute.
        self._arming_ledger_invalid = False
        self._reader = SqliteEconomicProjectionReader.from_repository(repo)
        # The previous tick's verdict, so an unchanged one is not re-logged.
        self._last_action: EnvelopeSyncAction | None = None
        # The previous tick's non-finite risk fields, deduplicated the same way.
        self._last_non_finite: tuple[str, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # The broker account the last successful read described. Under shadow
        # that is the LIVE account, while ``self._repo.account_id`` is the
        # ``shadow:`` custody namespace -- and every figure on a log line here
        # is the former's. ``None`` until the first read returns.
        self._observed_account_id: str | None = None

    async def observe(self) -> EnvelopeReading:
        """One broker read → the day-P&L reading, and the gate's observation.

        The observation is published only when the reading can be *judged*
        AND is not breached (ruling R-A′). A missing ``last_equity`` (plan R3),
        an external order observed today (plan R5), or a non-finite figure in
        cash, equity or unrealized P&L leaves the account unjudgeable, and an
        unjudgeable account must refuse every ENTER at once rather than let one
        be bounded against a cash figure nothing can vouch for — so such a tick
        withdraws whatever the last one published
        instead of republishing it. A *breached* reading withdraws for the
        same reason: nothing may take new exposure while the account is over
        its loss limit, so a fresh observation buys nothing, and publishing
        one before ``raise_account_hold`` has succeeded would leave the gate
        admitting on the cash bound alone if that raise failed.

        Raises ``BrokerError``: a failed read is not a verdict at all, so it
        never touches the gate and the last observation ages out on its own.
        """
        account, positions = await asyncio.gather(
            self._read.get_account(), self._read.list_positions()
        )
        self._observed_account_id = account.account_id
        observed_at_ms = self._repo.clock()
        # Under simulated custody the broker's cash never moved, so the
        # envelope subtracts what the Clerk's own fills would have spent
        # (plan R2); under real custody the broker's cash already reflects it.
        spent = (
            self._reader.account_net_cash_spent_usd()
            if self.envelope.custody_is_simulated
            else 0.0
        )
        unrealized_pl_usd = float(sum(position.unrealized_pl for position in positions))
        observation = AccountObservation(
            observed_at_ms=observed_at_ms,
            broker_cash_usd=account.cash,
            cash_available_usd=account.cash - spent,
            last_equity_usd=account.last_equity,
            unrealized_pl_usd=unrealized_pl_usd,
            position_count=len(positions),
        )
        # Withholding both loss inputs is the whole treatment: ``breached`` is
        # then None, the tick withdraws on the existing path, and no new
        # refusal code has to exist for a broker that reported a NaN.
        unjudgeable = self._noted_non_finite(
            _non_finite_risk_fields(
                cash=account.cash,
                last_equity=account.last_equity,
                unrealized_pl=unrealized_pl_usd,
            )
        )
        reading = EnvelopeReading(
            observation=observation,
            day_pnl=(
                None
                if unjudgeable
                else day_pnl_at(
                    self._reader, self._repo, observation=observation, now_ms=observed_at_ms
                )
            ),
            loss_limit_usd=(
                None
                if unjudgeable or account.last_equity is None
                else loss_limit_usd(self.envelope.values, last_equity_usd=account.last_equity)
            ),
        )
        if reading.breached is False:
            self.envelope.publish(observation)
        else:
            self.envelope.withdraw()
        return reading

    def _refresh_arming(self) -> ArmingSnapshot | None:
        """Re-read the account's arming ledger once; seal the envelope and refresh the gate.

        The envelope a live ENTER is admitted against is the one an operator
        sealed at arming (ADR 0059 D3/R10), and the instance it is admitted
        *for* must be armed right now (D11, slice 7). Both facts come from the
        same read, so one verdict can never describe two snapshots of a file an
        operator may be appending to.

        Read on every tick rather than once at composition because an arming
        is an out-of-process CLI act; one cadence is the whole latency between
        ``apply`` and the gate that enforces it.

        A ledger nobody can read seals nothing and admits nothing: the envelope
        returns to unsealed, the gate is invalidated with the fault, and the
        fault is logged at error level once per transition.
        """
        if self._arming_ledger is None:
            return None
        try:
            records = tuple(self._arming_ledger.records())
        except LiveArmingInvalid as exc:
            if not self._arming_ledger_invalid:
                logger.error(
                    "live arming ledger cannot be read; the envelope is unsealed and nothing is armed",
                    exc_info=True,
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": self._repo.account_id,
                        "live_account_id": self._arming_ledger.live_account_id,
                        "path": str(self._arming_ledger.path),
                        "why": str(exc),
                    },
                )
            self._arming_ledger_invalid = True
            self._assign_sealed(None)
            if self._arming_gate is not None:
                self._arming_gate.invalidate(str(exc))
            return None
        self._arming_ledger_invalid = False
        newest = latest_arming(records)
        self._assign_sealed(None if newest is None else newest.envelope)
        if self._arming_gate is None:
            return None
        snapshot = ArmingSnapshot(
            observed_at_ms=self._repo.clock(),
            live_account_id=self._arming_ledger.live_account_id,
            records=records,
            seals=dict(self._instance_seals()) if self._instance_seals is not None else {},
            configured_envelope=self.envelope.values,
        )
        if self._mode_disagreed:
            # The ledger is fine; the account is not the one this authority
            # was composed for. Nothing is admitted until a read agrees (R2).
            self._arming_gate.invalidate(
                "the broker-reported mode no longer agrees with the configured mode",
                reason_code=LIVE_MODE_DISAGREEMENT,
            )
        else:
            self._arming_gate.publish(snapshot)
        self._note_transitions(snapshot)
        return snapshot

    def _note_transitions(self, snapshot: ArmingSnapshot) -> None:
        """Warn once, with the code, for every instance that was armed last tick and is not now (R10).

        This is the loud half of ADR 0059 D8. The quiet half — new submission
        stops — is the ENTER refusal every such instance now gets; no desired
        state is written, so the instance keeps managing its own position.
        """
        armed_now = snapshot.armed_instance_ids(snapshot.observed_at_ms)
        for strategy_instance_id in sorted(self._previously_armed - armed_now):
            status = snapshot.status_for(strategy_instance_id, now_ms=snapshot.observed_at_ms)
            logger.warning(
                "%s: a live instance is no longer armed; its ENTERs refuse until it is re-armed",
                LIVE_VERDICT_TRANSITION_HALT,
                extra={
                    "action": "live_verdict_transition_halt",
                    "reason_code": status.reason_code,
                    "state": status.state,
                    "strategy_instance_id": strategy_instance_id,
                    "live_account_id": snapshot.live_account_id,
                    "account_id": self._repo.account_id,
                },
            )
        self._previously_armed = armed_now

    def _assign_sealed(self, sealed: LiveEnvelopeValues | None) -> None:
        """Assign the sealed envelope, logging each transition once (slice 6 R10)."""
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
                "account_id": self._repo.account_id,
                "live_account_id": self._arming_ledger.live_account_id if self._arming_ledger else None,
                "agreement": self.envelope.agreement,
            },
        )

    async def tick(self) -> EnvelopeSyncAction:
        """Observe once, and act on the verdict.

        The hold is checked before the breach so a standing hold is never
        re-raised: its cause is the breach it was raised on, and refreshing
        it with a later tick's numbers would churn the control revision and
        overwrite the evidence the operator is reading.
        """
        was_mode_disagreed = self._mode_disagreed
        self._refresh_arming()
        try:
            reading = await self.observe()
        except BrokerAccountModeDisagreement as exc:
            # Design R2: the account the read answered is not the one this
            # authority was composed for. Withdraw the observation (no ENTER
            # bounds against it) and hold the gate under the disagreement's
            # own code until a read agrees again; the next refresh does that.
            self._mode_disagreed = True
            self.envelope.withdraw()
            if self._arming_gate is not None:
                self._arming_gate.invalidate(exc.detail or str(exc), reason_code=LIVE_MODE_DISAGREEMENT)
            return self._acted("mode_disagreed", {"why": exc.detail or str(exc)})
        except BrokerError as exc:
            return self._acted("read_failed", {"why": str(exc)})
        self._mode_disagreed = False
        if was_mode_disagreed:
            # The refresh above ran before this tick's account read, so it
            # still judged the gate on the *previous* tick's disagreement and
            # held it invalid. This read just confirmed the mode agrees
            # again, so the gate is refreshed once more -- a second ledger
            # read, only on this rare recovery transition -- rather than
            # lagging the recovery a whole extra tick behind the account read
            # that already proved it safe.
            self._refresh_arming()
        if self._hold_stands():
            # A hold standing over an account that *also* cannot be judged is a
            # different operator situation from one over a judgeable account:
            # the second clears on the next guarded attempt, the first cannot
            # be attempted at all. Same verdict, so the same action -- but the
            # diagnosis rides along rather than being invisible.
            return self._acted(
                "hold_stands",
                {}
                if reading.breached is not None
                else {"why": "the account is also unjudgeable", **_unknown_detail(reading)},
            )
        if reading.breached is None:
            return self._acted("unknown", _unknown_detail(reading))
        cause = _breach_cause(reading)
        if cause is None:
            return self._acted("observed", _observed_detail(reading))
        outcome = raise_account_hold(
            self._repo,
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            evidence_refs=[f"day-pnl:{cause.day_start_ms}"],
            cause_facts=cause.to_mapping(),
        )
        return self._acted("hold_raised", {"outcome": outcome, **cause.to_mapping()})

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
        self._reader.close()


__all__ = ["EnvelopeReading", "EnvelopeSyncAction", "LiveEnvelopeSync"]
