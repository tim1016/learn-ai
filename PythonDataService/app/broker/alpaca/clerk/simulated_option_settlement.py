"""A simulation's own settlement of the option contracts it holds past their last session (ADR 0076 decision 22).

A real account's broker ends an expiring option position, and reports how
as an account activity. A Dry Run has no broker to do that, so its
simulation does it, as one explicit step: ``settle_expired_options``. The
Clerk's poll of option events calls the step on a simulated account's trade
port, and books what the step hands back through the same batch function
that books a real broker's activities.

**The step is a write, and it is not hidden in a read.** It changes the
simulation's own order ledger, so it lives on the trade port. The read port's
``read_option_event_activities`` stays a read: a simulation answers it with
none, and whole.

**What the step hands back** is everything the Clerk needs, read at one
moment under the ledger's own lock: each contract it settled, with the
simulation's record of how, and each contract it still holds past its last
session and could not settle, with why.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.broker.contract.option_events import BrokerOptionEventActivity, BrokerOptionShareTradeActivity


@dataclass(frozen=True, slots=True)
class SimulatedSettlement:
    """One contract a simulation settled, as the Clerk books it."""

    # The settlement in the shape an option event travels in, so the one rule that chooses lots chooses these.
    event: BrokerOptionEventActivity
    # The simulation's own record of the settlement: its rule, the price it read and where that came from.
    note: Mapping[str, Any]
    # The share trade an exercise made. None when it moved no shares.
    share_trade: BrokerOptionShareTradeActivity | None = None


@dataclass(frozen=True, slots=True)
class SimulatedOptionSettlements:
    """What one run of a simulation's settlement step did, and what it could not do."""

    # Every settlement in the window asked for, the ones made by earlier runs of the step among them.
    settled: tuple[SimulatedSettlement, ...] = ()
    # Each contract still held past its last session that the step could not settle, with why.
    not_settled: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class OptionSettlementSimulation(Protocol):
    """A trade port that may be a simulation's, and can be asked to settle what the simulation holds past expiry.

    A simulation's own port always answers with its settlements. The
    Clerk's guarded trade port answers None when the port behind it is no
    simulation: that account's broker ends its positions, and reports them
    through the read port.
    """

    def settle_expired_options(self, *, after_ms: int) -> SimulatedOptionSettlements | None:
        """Settle every contract held past its last session, and hand back the settlements dated in the window.

        ``after_ms`` is any instant on the window's first New York date, as
        for ``BrokerReadPort.read_option_event_activities``.
        """
        ...


def option_settlement_simulation(trade: object) -> OptionSettlementSimulation | None:
    """The settlement step a trade port declares, or None for a port that declares none."""
    return trade if isinstance(trade, OptionSettlementSimulation) else None


__all__ = [
    "OptionSettlementSimulation",
    "SimulatedOptionSettlements",
    "SimulatedSettlement",
    "option_settlement_simulation",
]
