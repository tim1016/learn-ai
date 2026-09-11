"""Durable arming invalidation when a different live envelope becomes effective.

Configuration events carry the hashes of permissions invalidated by Apply.
Historical arming records remain byte-identical, and changing back to an older
envelope cannot revive them. A new CLI ceremony produces a new permission.
The worker loads the fence once, before starting its execution tasks; operator
status reads use the same evidence through a read-only connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid, LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker_configuration.records import ConfigurationEvent, ProfileRevision
from app.broker_configuration.store import ProfilesStore, new_identifier, profiles_database_path

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
    from app.broker_configuration.worker_binding import BoundWorker

_ACTION = "live_arming_invalidated"


def _read_invalidations(conn: sqlite3.Connection, account_id: str) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in conn.execute(
            "SELECT previous_ref FROM configuration_events "
            "WHERE action = ? AND next_ref = ? AND previous_ref IS NOT NULL",
            (_ACTION, account_id),
        )
    )


def record_effective_arming_invalidations(
    store: ProfilesStore,
    conn: sqlite3.Connection,
    *,
    revision: ProfileRevision,
    account_id: str | None,
    actor: str,
    recorded_at_ms: int,
) -> None:
    """Append the refusal evidence inside the effective-selection transaction."""
    if revision.endpoint_mode != "live" or revision.live_envelope is None or account_id is None:
        return
    account_id = revision.account_pin or account_id
    ledger = LiveArmingLedger(store.db_path.parent.parent, live_account_id=account_id)
    already_invalid = _read_invalidations(conn, account_id)
    for record in ledger.records():
        if (
            isinstance(record, LiveArmingRecord)
            and record.envelope_sha256 != revision.live_envelope.sha
            and record.record_sha256 not in already_invalid
        ):
            store.append_event(conn, ConfigurationEvent(
                event_id=new_identifier("event"),
                actor_owner_id=actor,
                action=_ACTION,
                profile_id=revision.profile_id,
                revision=revision.revision,
                previous_ref=record.record_sha256,
                next_ref=account_id,
                result="invalidated",
                recorded_at_ms=recorded_at_ms,
            ))


def configuration_arming_invalidations(clerk_dir: Path, account_id: str) -> frozenset[str]:
    """Read configuration evidence without creating or migrating a database."""
    path = profiles_database_path(clerk_dir)
    if not path.exists():
        return frozenset()
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return _read_invalidations(conn, account_id)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise LiveArmingInvalid("configuration arming invalidation evidence cannot be read") from exc


def install_configuration_arming_fence(*, bound: BoundWorker, gate: ArmingGate | None) -> None:
    """Install after acknowledgement, before execution tasks can publish a snapshot."""
    if gate is None or bound.context.account_pin is None:
        return
    gate.set_configuration_invalidations(configuration_arming_invalidations(
        bound.context.settings.clerk_dir, bound.context.account_pin
    ))
