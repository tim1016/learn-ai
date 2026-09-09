"""The per-instance arming gate a live authority admits ENTERs against (ADR 0059 D3/D11, slice 7).

Formula: ``status_for(sid, now_ms) = arming_status(records, seal_hash=seals[sid], now_ms)``
  over one snapshot of the ledger and the runner's sealed bindings; the
  snapshot is fresh iff ``0 <= now_ms - observed_at_ms <= max_age``.
Reference: design R5 in
  ``docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md``.
Canonical implementation: this file. The status rule is ``live_arming.py``;
  the refresh is ``sqlite/live_envelope_sync.py``; the admission is
  ``sqlite/arming_admission.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_arming_gate.py``.

Pure: no I/O, no clock. The sync publishes a snapshot it stamped with the
repository clock; admission derives the instance's state at *its own*
``now_ms``, so a lapse at the ET-date boundary is enforced at the instant.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_LEDGER_INVALID,
    ArmingStatus,
    LedgerRecord,
    arming_status,
    instance_ids,
)
from app.broker.alpaca.clerk.live_envelope import OBSERVATION_MAX_AGE_MS, LiveEnvelopeValues


@dataclass(frozen=True)
class ArmingSnapshot:
    """One verified read of the ledger and the sealed bindings, at one instant."""

    observed_at_ms: int
    live_account_id: str
    records: tuple[LedgerRecord, ...]
    # ``strategy_instance_id`` -> the instance's current sealed-program hash.
    # An instance absent here has no sealed binding on this account any more,
    # which ``arming_status`` reads as a change from whatever was armed.
    seals: Mapping[str, str]
    configured_envelope: LiveEnvelopeValues

    def status_for(self, strategy_instance_id: str, *, now_ms: int) -> ArmingStatus:
        return arming_status(
            self.records,
            live_account_id=self.live_account_id,
            strategy_instance_id=strategy_instance_id,
            seal_hash=self.seals.get(strategy_instance_id),
            configured_envelope=self.configured_envelope,
            now_ms=now_ms,
        )

    def armed_instance_ids(self, now_ms: int) -> frozenset[str]:
        """Every instance the ledger names that is ``armed`` at ``now_ms``."""
        return frozenset(
            sid for sid in instance_ids(self.records) if self.status_for(sid, now_ms=now_ms).state == "armed"
        )


class ArmingGate:
    """The snapshot one authority admits ENTERs against.

    A process-local cache of local evidence, never a custody fact — the
    ``LiveEnvelopeGate`` precedent. ``invalidate`` drops the snapshot so a
    ledger nobody can read — or a broker whose mode no longer agrees (R2) —
    admits nothing, and says why under which code.
    """

    def __init__(self, *, observation_max_age_ms: int = OBSERVATION_MAX_AGE_MS) -> None:
        self._max_age_ms = observation_max_age_ms
        self._snapshot: ArmingSnapshot | None = None
        self._invalid: tuple[str, str] | None = None

    def publish(self, snapshot: ArmingSnapshot) -> None:
        self._snapshot = snapshot
        self._invalid = None

    def invalidate(self, why: str, *, reason_code: str = LIVE_ARMING_LEDGER_INVALID) -> None:
        self._snapshot = None
        self._invalid = (reason_code, why)

    @property
    def invalid_why(self) -> str | None:
        return None if self._invalid is None else self._invalid[1]

    @property
    def invalid_reason_code(self) -> str | None:
        return None if self._invalid is None else self._invalid[0]

    def latest_snapshot(self) -> ArmingSnapshot | None:
        return self._snapshot

    def fresh_snapshot(self, now_ms: int) -> ArmingSnapshot | None:
        """The published snapshot, if its age at ``now_ms`` is in range.

        Fresh means ``0 <= age <= max_age`` — the envelope's rule: a snapshot
        dated after the admission clock is not fresh either.
        """
        snapshot = self._snapshot
        if snapshot is None or not (0 <= now_ms - snapshot.observed_at_ms <= self._max_age_ms):
            return None
        return snapshot


__all__ = ["ArmingGate", "ArmingSnapshot"]
