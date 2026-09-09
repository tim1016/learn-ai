"""Independent fixed-cadence lifecycle for the live envelope (ADR 0059 D4).

Modelled on ``StreamHealthHoldSync``: one background tap produces the
account observation and the loss hold; ``accept_enter`` consumes them and
never contacts the broker. Decoupled from the reconciliation pass, whose
backoff reaches 300 s on failure — exactly when a loss hold matters most.

The sync raises the loss hold and never releases it: only the guarded
operator action does (plan R6, R12).

Two facts leave the account *unjudgeable* rather than merely unlucky: a
broker snapshot with no ``last_equity`` has no loss limit to judge against
(plan R3), and an external order observed today makes the day's P&L
unknowable (plan R5). Neither may be read as "nothing breached", so an
unjudgeable tick withdraws the gate's observation and every ENTER refuses
``LIVE_ENVELOPE_UNOBSERVED`` until a judgeable one arrives.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SYNC_INTERVAL_S,
    AccountObservation,
    LiveEnvelopeGate,
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
from app.broker.contract.errors import BrokerError
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]
EnvelopeSyncAction = Literal["observed", "hold_raised", "hold_stands", "unknown", "read_failed"]

# What each verdict says to an operator, and how loudly. The three warnings
# are the ones that change what the account will accept.
_ACTION_MESSAGES: dict[EnvelopeSyncAction, str] = {
    "observed": "live envelope observed the account",
    "hold_raised": "live envelope raised the loss hold",
    "hold_stands": "live envelope loss hold still stands",
    "unknown": "live envelope cannot judge the account; every ENTER is refused",
    "read_failed": "live envelope could not read the account; the observation will age out",
}
_WARNING_ACTIONS: frozenset[str] = frozenset({"hold_raised", "read_failed", "unknown"})


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
    ) -> None:
        self._repo = repo
        self._read = read
        self.envelope = envelope
        self._interval_s = interval_s
        self._sleep = sleep
        self._max_ticks = max_ticks
        self._reader = SqliteEconomicProjectionReader.from_repository(repo)
        # The previous tick's verdict, so an unchanged one is not re-logged.
        self._last_action: EnvelopeSyncAction | None = None
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
        AND is not breached (ruling R-A′). A missing ``last_equity`` (plan R3)
        or an external order observed today (plan R5) leaves the account
        unjudgeable, and an unjudgeable account must refuse every ENTER at
        once rather than let one be bounded against a cash figure nothing can
        vouch for — so such a tick withdraws whatever the last one published
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
        observation = AccountObservation(
            observed_at_ms=observed_at_ms,
            broker_cash_usd=account.cash,
            cash_available_usd=account.cash - spent,
            last_equity_usd=account.last_equity,
            unrealized_pl_usd=float(sum(position.unrealized_pl for position in positions)),
            position_count=len(positions),
        )
        reading = EnvelopeReading(
            observation=observation,
            day_pnl=day_pnl_at(
                self._reader, self._repo, observation=observation, now_ms=observed_at_ms
            ),
            loss_limit_usd=(
                None
                if account.last_equity is None
                else loss_limit_usd(self.envelope.values, last_equity_usd=account.last_equity)
            ),
        )
        if reading.breached is False:
            self.envelope.publish(observation)
        else:
            self.envelope.withdraw()
        return reading

    async def tick(self) -> EnvelopeSyncAction:
        """Observe once, and act on the verdict.

        The hold is checked before the breach so a standing hold is never
        re-raised: its cause is the breach it was raised on, and refreshing
        it with a later tick's numbers would churn the control revision and
        overwrite the evidence the operator is reading.
        """
        try:
            reading = await self.observe()
        except BrokerError as exc:
            return self._acted("read_failed", {"why": str(exc)})
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
