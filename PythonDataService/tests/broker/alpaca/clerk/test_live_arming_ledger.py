"""The append-only, account-rooted arming ledger (ADR 0059 D3, design R3)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import app.broker.alpaca.clerk.live_arming_ledger as ledger_module
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    instance_ids,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LIVE_ARMING_FILENAME, LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.services.session_authority import et_minute_of_day_ms
from app.utils.advisory_lock import try_advisory_file_lock

ACCOUNT = "9LIVE0001"
OTHER_ACCOUNT = "9LIVE0002"
SID = "ema-shadow-1"
ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)
FRIDAY_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)


def _armed(
    *,
    account: str = ACCOUNT,
    instance: str = SID,
    armed_at_ms: int = FRIDAY_MS,
    envelope: LiveEnvelopeValues = ENVELOPE,
) -> LiveArmingRecord:
    return LiveArmingRecord.create(
        live_account_id=account,
        strategy_instance_id=instance,
        seal_hash="a" * 64,
        configured_signal_hash="b" * 64,
        shadow_receipt_sha256="c" * 64,
        envelope=envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=20,
    )


def _row_json(record: LiveArmingRecord) -> str:
    return json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def test_the_ledger_is_account_rooted_outside_every_custody_namespace(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    assert ledger.path == tmp_path / "accounts" / "arming" / ACCOUNT / LIVE_ARMING_FILENAME
    assert ledger.live_account_id == ACCOUNT
    # Not under accounts/alpaca/ and not inside a custody namespace directory:
    # no cutover or latent-database check can mistake this tree for an authority.
    assert "alpaca" not in ledger.path.parts
    assert ledger.records() == () and instance_ids(ledger.records()) == ()
    assert ledger.latest(SID) is None and latest_arming(ledger.records()) is None


def test_a_reserved_namespace_account_never_gets_a_ledger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved"):
        LiveArmingLedger(tmp_path, live_account_id=f"shadow:{ACCOUNT}")


def test_append_and_read_keep_file_order_and_survive_a_reopen(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    first = _armed()
    second = _armed(instance="ema-shadow-2", armed_at_ms=FRIDAY_MS + 1)
    ledger.append(first)
    ledger.append(second)

    reopened = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    assert reopened.records() == (first, second)
    assert reopened.records_for(SID) == (first,)
    assert reopened.latest(SID) == first
    assert latest_arming(reopened.records()) == second
    assert instance_ids(reopened.records()) == (SID, "ema-shadow-2")
    assert len(reopened.path.read_text(encoding="utf-8").splitlines()) == 2


def test_appending_version_one_preserves_its_original_payload_and_digest(tmp_path: Path) -> None:
    """A rollback must still read rows written by the widened model."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    record = _armed()

    ledger.append(record)

    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    unsigned = {
        key: value
        for key, value in asdict(record).items()
        if key not in {"record_sha256", "predecessor", "originating_plan_id"}
    }
    expected = {**unsigned, "record_sha256": record.record_sha256}
    assert payload == expected
    assert ledger.path.read_text(encoding="utf-8") == (
        json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    )
    assert record.record_sha256 == canonical_sha256(unsigned)


def test_the_sealed_envelope_is_the_latest_arming_records_own(tmp_path: Path) -> None:
    """The seal is ``latest_arming(records())``'s envelope -- the one read the sync takes."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    latest = latest_arming(ledger.records())
    assert latest is not None and latest.envelope == ENVELOPE

    tightened = replace(ENVELOPE, loss_usd=4_000.0)
    ledger.append(_armed(instance="ema-shadow-2", armed_at_ms=FRIDAY_MS + 1, envelope=tightened))
    latest = latest_arming(ledger.records())
    assert latest is not None and latest.envelope == tightened


def test_a_disarm_never_unseals_the_account_level_envelope(tmp_path: Path) -> None:
    """R10 seals from the latest *arming* record; a revocation is per instance."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    record = _armed()
    ledger.append(record)
    ledger.append(
        LiveDisarmRecord.create(
            live_account_id=ACCOUNT,
            strategy_instance_id=SID,
            revokes_record_sha256=record.record_sha256,
            disarmed_at_ms=FRIDAY_MS + 1,
        )
    )

    assert isinstance(ledger.latest(SID), LiveDisarmRecord)
    assert latest_arming(ledger.records()) == record
    assert record.envelope == ENVELOPE


def test_a_foreign_accounts_row_is_ignored_rather_than_answered_for(tmp_path: Path) -> None:
    """The tree is account-rooted, so a foreign row can only be hand-planted."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8") + _row_json(_armed(account=OTHER_ACCOUNT)),
        encoding="utf-8",
    )

    assert ledger.records() == (_armed(),)
    assert instance_ids(ledger.records()) == (SID,)


def test_appending_another_accounts_record_is_refused_before_the_write(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    with pytest.raises(LiveArmingInvalid, match="belongs to another live account"):
        ledger.append(_armed(account=OTHER_ACCOUNT))

    assert not ledger.path.exists()


def test_a_symlinked_ledger_is_refused_on_both_read_and_append(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "elsewhere.jsonl").write_text("", encoding="utf-8")
    ledger.path.symlink_to(tmp_path / "elsewhere.jsonl")

    with pytest.raises(LiveArmingInvalid, match="must be a regular file"):
        ledger.records()
    with pytest.raises(LiveArmingInvalid, match="must be a regular file"):
        ledger.append(_armed())


def test_a_tampered_row_is_refused_rather_than_read(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace(
            f'"armed_at_ms":{FRIDAY_MS}', f'"armed_at_ms":{FRIDAY_MS + 1}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        ledger.records()


def test_a_row_with_no_recognised_kind_is_refused(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.append(_armed())
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace('"kind":"armed"', '"kind":"maybe"'),
        encoding="utf-8",
    )

    with pytest.raises(LiveArmingInvalid, match="unrecognised kind"):
        ledger.records()


def test_revoking_reads_and_appends_under_one_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deciding what is revoked and writing the revocation are one transaction.

    Split across two lock acquisitions, a concurrent re-arm lands between them
    and ``revokes_record_sha256`` names a record that is no longer the latest;
    two concurrent disarms both succeed. The effective state and the audit
    evidence then disagree about a real-money permission.

    ``flock`` binds to the open file description, so a second ``open`` in this
    same process is a genuine second contender: each probe below would acquire
    the lock if it were not already held across both halves.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    armed = _armed()
    ledger.append(armed)
    observed: list[tuple[str, bool]] = []
    real_read = ledger_module.read_canonical_jsonl_objects
    real_append = ledger_module.append_canonical_jsonl_line

    def _probing_read(path: Path, *, invalid: type[ValueError], label: str) -> list[Mapping[str, Any]]:
        with try_advisory_file_lock(path) as acquired:
            observed.append(("read", acquired))
        return real_read(path, invalid=invalid, label=label)

    def _probing_append(path: Path, payload: dict, *, invalid: type[ValueError], label: str) -> None:
        with try_advisory_file_lock(path) as acquired:
            observed.append(("append", acquired))
        real_append(path, payload, invalid=invalid, label=label)

    monkeypatch.setattr(ledger_module, "read_canonical_jsonl_objects", _probing_read)
    monkeypatch.setattr(ledger_module, "append_canonical_jsonl_line", _probing_append)
    revocation = ledger.revoke_latest(SID, disarmed_at_ms=FRIDAY_MS + 1)

    assert observed == [("read", False), ("append", False)]
    assert revocation.revokes_record_sha256 == armed.record_sha256
    assert revocation.disarmed_at_ms == FRIDAY_MS + 1


def test_revoking_an_instance_with_no_arming_row_refuses_and_writes_nothing(tmp_path: Path) -> None:
    """The same refusal on a never-armed instance and on an already-revoked one."""
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    with pytest.raises(LiveArmingRefused) as caught:
        ledger.revoke_latest(SID, disarmed_at_ms=FRIDAY_MS)
    assert caught.value.reason_code == LIVE_ARMING_NOT_ARMED
    assert not ledger.path.exists()

    ledger.append(_armed())
    ledger.revoke_latest(SID, disarmed_at_ms=FRIDAY_MS + 1)
    with pytest.raises(LiveArmingRefused) as second:
        ledger.revoke_latest(SID, disarmed_at_ms=FRIDAY_MS + 2)

    assert second.value.reason_code == LIVE_ARMING_NOT_ARMED
    assert [type(row) for row in ledger.records()] == [LiveArmingRecord, LiveDisarmRecord]


def test_a_refused_revoke_on_a_fresh_root_creates_no_arming_directory(tmp_path: Path) -> None:
    """A refusal must precede the lock, so it leaves no trace on disk.

    ``advisory_file_lock`` mkdirs the ledger's parent directory (and would
    create the sibling ``.live_arming.jsonl.lock`` file) before any guard
    inside the ``with`` block can run. A ledger file that does not exist yet
    cannot hold an arming row to revoke, so ``revoke_latest`` must refuse
    before taking the lock at all -- not just before writing a row.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)

    with pytest.raises(LiveArmingRefused) as caught:
        ledger.revoke_latest(SID, disarmed_at_ms=FRIDAY_MS)

    assert caught.value.reason_code == LIVE_ARMING_NOT_ARMED
    assert not (tmp_path / "accounts" / "arming").exists()


def test_discover_finds_the_one_account_whose_ledger_names_the_instance(tmp_path: Path) -> None:
    """The arming tree answers without the shadow activation proof."""
    LiveArmingLedger(tmp_path, live_account_id=OTHER_ACCOUNT).append(
        _armed(account=OTHER_ACCOUNT, instance="somebody-else")
    )
    LiveArmingLedger(tmp_path, live_account_id=ACCOUNT).append(_armed())

    found = LiveArmingLedger.discover(tmp_path, strategy_instance_id=SID)

    assert found is not None and found.live_account_id == ACCOUNT
    assert LiveArmingLedger.discover(tmp_path, strategy_instance_id="never-armed") is None
    assert LiveArmingLedger.discover(tmp_path / "empty", strategy_instance_id=SID) is None


def test_discover_skips_an_unreadable_ledger_loudly_rather_than_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A damaged sibling must neither hide a readable ledger nor vanish quietly."""
    damaged = LiveArmingLedger(tmp_path, live_account_id=OTHER_ACCOUNT)
    damaged.append(_armed(account=OTHER_ACCOUNT))
    damaged.path.write_text(
        damaged.path.read_text(encoding="utf-8").replace(
            f'"armed_at_ms":{FRIDAY_MS}', f'"armed_at_ms":{FRIDAY_MS + 1}'
        ),
        encoding="utf-8",
    )
    LiveArmingLedger(tmp_path, live_account_id=ACCOUNT).append(_armed())

    with caplog.at_level(logging.ERROR):
        found = LiveArmingLedger.discover(tmp_path, strategy_instance_id=SID)

    assert found is not None and found.live_account_id == ACCOUNT
    (invalid,) = [
        record for record in caplog.records if getattr(record, "action", None) == "live_arming_ledger_invalid"
    ]
    assert invalid.account_id == OTHER_ACCOUNT  # type: ignore[attr-defined]
    assert invalid.exc_info is not None, "the traceback names which row will not verify"


def test_discover_refuses_when_two_accounts_armed_the_same_instance(tmp_path: Path) -> None:
    """Nothing in the tree can choose between them, so it does not choose."""
    LiveArmingLedger(tmp_path, live_account_id=ACCOUNT).append(_armed())
    LiveArmingLedger(tmp_path, live_account_id=OTHER_ACCOUNT).append(_armed(account=OTHER_ACCOUNT))

    with pytest.raises(LiveArmingRefused) as caught:
        LiveArmingLedger.discover(tmp_path, strategy_instance_id=SID)

    assert caught.value.reason_code == LIVE_ARMING_INSTANCE_UNSEALED
    assert ACCOUNT in str(caught.value) and OTHER_ACCOUNT in str(caught.value)


def test_the_append_holds_the_advisory_lock_across_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writers appending at once must not interleave a row.

    ``flock`` binds to the open file description, so a second ``open`` in this
    same process is a genuine second contender: if the lock were not held here,
    the probe below would acquire it.
    """
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    observed: list[bool] = []
    real_append = ledger_module.append_canonical_jsonl_line

    def _probe(path: Path, payload: dict, *, invalid: type[ValueError], label: str) -> None:
        with try_advisory_file_lock(path) as acquired:
            observed.append(acquired)
        real_append(path, payload, invalid=invalid, label=label)

    monkeypatch.setattr(ledger_module, "append_canonical_jsonl_line", _probe)
    ledger.append(_armed())

    assert observed == [False], "append must hold the advisory lock while it writes"
    assert len(ledger.records()) == 1
