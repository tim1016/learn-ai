"""One ledger read per tick: the sealed envelope, the arming snapshot, the halt warning.

The arming half of the envelope sync's cadence (ADR 0059 D3/D11, slice 7 R5,
R10), lifted out of ``live_envelope_sync.py`` so that module keeps its one
stated subject. The envelope a live ENTER is admitted against is the one an
operator sealed at arming, and the instance it is admitted *for* must be armed
right now; both facts come from the same ``records()`` tuple, so one verdict
can never describe two snapshots of a file an operator may be appending to.
That single-read invariant is what this collaborator exists to hold.

Read on every tick rather than once at composition because an arming is an
out-of-process CLI act; one cadence is the whole latency between ``apply`` and
the gate that enforces it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from app.broker.alpaca.clerk.live_arming import (
    LIVE_VERDICT_TRANSITION_HALT,
    LiveArmingInvalid,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate, ArmingSnapshot
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues

logger = logging.getLogger(__name__)

# The runner's sealed bindings, read per tick: ``strategy_instance_id`` -> the
# instance's current sealed-program hash. Injected as a callable so the clerk
# layer stays free of bot-registration imports (the ``roster_symbols`` pattern).
type InstanceSeals = Callable[[], Mapping[str, str]]


class ArmingRefresh:
    """Re-reads one account's arming ledger, seals the envelope and refreshes the gate.

    Constructed only where an arming ledger exists; the sync holds ``None``
    otherwise. ``gate`` and ``instance_seals`` are absent on the shadow
    authority, which seals the envelope but admits no ENTER against arming
    (slice 6): with no gate this refresh reads the ledger and returns the
    sealed envelope, and nothing else.
    """

    def __init__(
        self,
        *,
        ledger: LiveArmingLedger,
        gate: ArmingGate | None = None,
        instance_seals: InstanceSeals | None = None,
        account_id: str,
    ) -> None:
        self._ledger = ledger
        self._gate = gate
        self._instance_seals = instance_seals
        # The custody account this Clerk writes against. Under shadow that is
        # ``shadow:<id>`` while the ledger is rooted at the live ``<id>``, and
        # every log line here carries both so neither is guessed.
        self._account_id = account_id
        # The instances the previous refresh judged armed, so an instance
        # leaving that set is warned about exactly once (ADR 0059 D8 as R10).
        self._previously_armed: frozenset[str] = frozenset()
        # Whether the last refresh found the ledger unreadable, so the error is
        # logged once per transition rather than four times a minute.
        self._ledger_invalid = False

    @property
    def live_account_id(self) -> str:
        return self._ledger.live_account_id

    @property
    def inputs_unreadable(self) -> bool:
        """Whether the last refresh could not read this account's arming inputs.

        Read by the envelope sync: an account whose seal cannot be read cannot
        be *judged* either, because the sealed envelope is what its loss limit
        comes from (ADR 0059 D3).
        """
        return self._ledger_invalid

    def _read_seals(self) -> dict[str, str]:
        """The runner's sealed bindings, or one named fault when they cannot be read.

        The callable reaches the runner's binding store on disk, so it can fail
        exactly the way the ledger can. A refresh that cannot read *either*
        input knows nothing about what is armed, and must say so under this
        module's own fault rather than escaping as an unhandled error: the
        guarded loss-hold clear re-observes through this path, and an escaping
        ``OSError`` would answer HTTP 500 where the contract is "refused, the
        hold stands".
        """
        if self._instance_seals is None:
            return {}
        try:
            return dict(self._instance_seals())
        except (OSError, ValueError) as exc:
            raise LiveArmingInvalid("the runner's sealed bindings cannot be read") from exc

    def refresh(self, now_ms: int, configured_envelope: LiveEnvelopeValues) -> LiveEnvelopeValues | None:
        """Read the ledger once; publish the snapshot and return the sealed envelope.

        Arming inputs nobody can read seal nothing and admit nothing: the caller
        gets ``None`` (the envelope returns to unsealed), the gate is
        invalidated with the fault, and the fault is logged at error level once
        per transition. Both inputs are read under one fault because a refresh
        holding only half of them can describe neither.
        """
        input_read = "ledger"
        try:
            records = tuple(self._ledger.records())
            input_read = "instance_seals"
            seals = self._read_seals()
        except LiveArmingInvalid as exc:
            if not self._ledger_invalid:
                logger.error(
                    "live arming inputs cannot be read; the envelope is unsealed and nothing is armed",
                    exc_info=True,
                    extra={
                        "action": "live_arming_ledger_invalid",
                        "account_id": self._account_id,
                        "live_account_id": self._ledger.live_account_id,
                        # Which of the two inputs failed, and the ledger's path
                        # only when it is the one at fault -- stamping a healthy
                        # file beside a binding-store error sends the operator
                        # to the wrong place.
                        "input": input_read,
                        "path": str(self._ledger.path) if input_read == "ledger" else None,
                        "why": str(exc),
                    },
                )
            self._ledger_invalid = True
            if self._gate is not None:
                self._gate.invalidate(str(exc))
            return None
        self._ledger_invalid = False
        newest = latest_arming(records)
        sealed = None if newest is None else newest.envelope
        if self._gate is None:
            return sealed
        snapshot = ArmingSnapshot(
            observed_at_ms=now_ms,
            live_account_id=self._ledger.live_account_id,
            records=records,
            seals=seals,
            configured_envelope=configured_envelope,
        )
        self._gate.publish(snapshot)
        self._note_transitions(snapshot)
        return sealed

    def _note_transitions(self, snapshot: ArmingSnapshot) -> None:
        """Warn once, with the code, for every instance this snapshot's ledger stopped calling armed (R10).

        The set tracked is *the ledger's* armed set, not what the gate admits:
        this runs on every readable refresh, including one whose gate is held
        under a mode disagreement. Deliberate — an instance does not re-enter
        the armed set when a disagreement clears, so tracking the gate's view
        would replay every warning as a recovery-time storm.

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
                    "account_id": self._account_id,
                },
            )
        self._previously_armed = armed_now


__all__ = ["ArmingRefresh", "InstanceSeals"]
