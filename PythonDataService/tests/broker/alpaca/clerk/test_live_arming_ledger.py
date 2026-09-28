"""The append-only, account-rooted arming ledger (ADR 0059 D3, design R3)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INSTANCE_UNSEALED,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    instance_ids,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LIVE_ARMING_FILENAME
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.historical_arming import HistoricalArmingLedger as LiveArmingLedger

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
        if key not in {"record_sha256", "predecessor", "originating_plan_id", "exit_terms"}
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




def test_a_symlinked_ledger_is_refused_on_both_read_and_append(tmp_path: Path) -> None:
    ledger = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "elsewhere.jsonl").write_text("", encoding="utf-8")
    ledger.path.symlink_to(tmp_path / "elsewhere.jsonl")

    with pytest.raises(LiveArmingInvalid, match="must be a regular file"):
        ledger.records()


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




def test_production_ledger_has_no_mutating_arming_interface(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger as ReadOnlyLedger

    ledger = ReadOnlyLedger(tmp_path, live_account_id=ACCOUNT)
    assert not any(hasattr(ledger, name) for name in ("append", "append_once_for_plan", "revoke_latest"))
    fixture = LiveArmingLedger(tmp_path, live_account_id=ACCOUNT)
    fixture.append(_armed())
    before = fixture.path.read_bytes()
    assert ledger.latest(SID) == _armed()
    assert fixture.path.read_bytes() == before
