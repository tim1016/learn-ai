"""Read-only interpretation of historical arming evidence.

Budget authority cutover retires all grant-writing ceremonies. These readers
preserve sealed records and support the pre-cutover compatibility boundary;
they never create a permission or alter a ledger.
"""


from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.account_authority import (
    custody_account_id_for,
    custody_account_ids_for,
)
from app.broker.alpaca.clerk.live_arming import (
    ArmingStatus,
    LiveArmingRecord,
    arming_status,
    instance_ids,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.schemas.account_authority import CustodyWorld
from app.services.bot_binding_repository import live_state_binding_repository


@dataclass(frozen=True)
class InstanceSeal:
    """One instance's two hashes: what arming binds to, and what the receipt binds to."""

    seal_hash: str
    configured_signal_hash: str



def instance_seal_hashes(
    *,
    live_account_id: str,
    live_state_root: Path,
    custody_world: CustodyWorld | None = None,
) -> dict[str, InstanceSeal]:
    """Every sealed Alpaca instance bound to this live account, by instance id.

    A binding with no v2 program seal is skipped rather than refused: it is a
    legacy record that cannot be armed, and its presence must not stop a sealed
    sibling from arming.

    ``custody_world`` narrows the admissible custody ids to that world's own
    -- whatever ``custody_account_id_for`` answers for it, so ``real_live``
    means the live id alone. After graduation the rehearsal's ``shadow:``-sealed
    bindings stay on disk (design R15) and their slice-6 arming records stay
    in the same account-rooted ledger, so a reader that counts them would
    report instances the live authority does not custody -- and refuses on
    every Start -- as armed under it. UI and CLI status pass the active world.
    ``None`` (the plan ceremony) keeps both ids: arming a shadow-sealed instance under a graduated
    account grants nothing and is refused at Start anyway.
    """
    # The world-to-custody-id rule is ``custody_account_id_for``'s, so naming a
    # world here narrows to that world's own id for *any* world rather than
    # re-spelling the ``real_live`` half of the table in a feature module.
    admissible = (
        custody_account_ids_for(live_account_id)
        if custody_world is None
        else frozenset({custody_account_id_for(custody_world, live_account_id)})
    )
    seals: dict[str, InstanceSeal] = {}
    for binding in live_state_binding_repository(live_state_root).list_for_broker("alpaca"):
        seal = binding.sealed_program
        if seal is None or binding.sealed_account_id not in admissible:
            continue
        seals[binding.strategy_instance_id] = InstanceSeal(
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
        )
    return seals



@dataclass(frozen=True)
class AccountArming:
    """One live account's arming evidence, from a single read of its ledger.

    Every fact R11 publishes about an account -- the per-instance statuses, the
    record that sealed its envelope, the armed count and the envelope state --
    is derived here from the same ``records()`` tuple. Two readers of one
    verdict can therefore never describe two different snapshots of a file an
    operator may be appending to while the verdict is being composed.
    """

    statuses: dict[str, ArmingStatus]
    sealed: LiveArmingRecord | None

    @property
    def armed_instance_count(self) -> int:
        """How many of the named instances are armed at the judged instant (R11)."""
        return sum(1 for status in self.statuses.values() if status.state == "armed")

    @property
    def envelope_state(self) -> Literal["configured_unsealed", "sealed"]:
        """Whether any arming record has sealed this account's envelope (R11)."""
        return "configured_unsealed" if self.sealed is None else "sealed"



def account_arming(
    *,
    live_account_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
    strategy_instance_ids: Sequence[str] | None = None,
    custody_world: CustodyWorld | None = None,
) -> AccountArming:
    """This account's arming evidence, reading the ledger file exactly once.

    ``strategy_instance_ids=None`` means "every instance with a row in the
    ledger" -- the set an operator's ``status`` and the live verdict both want.
    An instance whose sealed binding has gone gets ``seal_hash=None``, which
    ``arming_status`` reads as a change from what was armed.

    The runner's bindings are read only when the ledger actually knows one of
    the wanted instances: nothing under ``live_state_root`` can change the
    answer for an instance that was never armed, so a ``status`` on a
    never-armed account touches no bindings root at all -- which on a fresh
    installation may not exist yet.

    ``custody_world`` passes straight through to :func:`instance_seal_hashes`:
    a caller that knows it reads the ``real_live`` world counts only
    live-sealed instances.
    """
    from app.broker_configuration.arming_policy import configuration_arming_invalidations

    invalidated = configuration_arming_invalidations(artifacts_root, live_account_id)
    records = LiveArmingLedger(artifacts_root, live_account_id=live_account_id).records()
    known = instance_ids(records)
    wanted = known if strategy_instance_ids is None else tuple(strategy_instance_ids)
    seals = (
        instance_seal_hashes(
            live_account_id=live_account_id,
            live_state_root=live_state_root,
            custody_world=custody_world,
        )
        if any(sid in known for sid in wanted)
        else {}
    )
    statuses: dict[str, ArmingStatus] = {}
    for sid in wanted:
        seal = seals.get(sid)
        statuses[sid] = arming_status(
            records,
            live_account_id=live_account_id,
            strategy_instance_id=sid,
            seal_hash=None if seal is None else seal.seal_hash,
            configured_envelope=configured_envelope,
            now_ms=now_ms,
            invalidated_record_shas=invalidated,
        )
    return AccountArming(statuses=statuses, sealed=latest_arming(records))

