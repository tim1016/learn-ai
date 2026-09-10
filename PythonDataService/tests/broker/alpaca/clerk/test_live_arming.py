"""The sealed arming record, its revocation, and the pure status rule (ADR 0059 D3)."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date

import pytest

from app.broker.alpaca.clerk.live_arming import (
    ARMING_REASON_CODES,
    LIVE_ARMING_FUTURE_DATED,
    LIVE_ARMING_LAPSED,
    LIVE_ARMING_REVOKED,
    LIVE_ARMING_SEAL_CHANGED,
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_MISSING,
    ArmingStatus,
    LedgerRecord,
    LiveArmingInvalid,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    RehearsalPredecessor,
    arming_status,
    sessions_used,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.lean_sidecar.trading_calendar import is_trading_day, trading_session_count
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import MAX_TIMESTAMP_MS

ACCOUNT = "9LIVE0001"
SID = "ema-shadow-1"
SEAL = "a" * 64
SIGNAL = "b" * 64
RECEIPT = "c" * 64
ENVELOPE = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)
# Friday 2026-09-11, 10:00 ET — a full NYSE session.
FRIDAY_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)
MONDAY_MS = et_minute_of_day_ms(date(2026, 9, 14), 10 * 60)
TUESDAY_MS = et_minute_of_day_ms(date(2026, 9, 15), 10 * 60)


def _armed(*, max_sessions: int = 20, armed_at_ms: int = FRIDAY_MS, seal: str = SEAL, instance: str = SID,
           account: str = ACCOUNT, envelope: LiveEnvelopeValues | None = None) -> LiveArmingRecord:
    """One armed row whose sealed envelope grants exactly what the row claims.

    ``max_sessions`` and the sealed envelope's ``arming_max_sessions`` are one
    number, so varying the grant here varies the envelope with it. Supplying
    ``envelope`` explicitly is how a test forces the two apart.
    """
    return LiveArmingRecord.create(
        live_account_id=account,
        strategy_instance_id=instance,
        seal_hash=seal,
        configured_signal_hash=SIGNAL,
        shadow_receipt_sha256=RECEIPT,
        envelope=replace(ENVELOPE, arming_max_sessions=max_sessions) if envelope is None else envelope,
        armed_at_ms=armed_at_ms,
        max_sessions=max_sessions,
    )


def _disarmed(record: LiveArmingRecord, *, at_ms: int = MONDAY_MS) -> LiveDisarmRecord:
    return LiveDisarmRecord.create(
        live_account_id=record.live_account_id,
        strategy_instance_id=record.strategy_instance_id,
        revokes_record_sha256=record.record_sha256,
        disarmed_at_ms=at_ms,
    )


def _status(records: list[LedgerRecord], **overrides: object) -> ArmingStatus:
    kwargs: dict = {
        "live_account_id": ACCOUNT,
        "strategy_instance_id": SID,
        "seal_hash": SEAL,
        "configured_envelope": ENVELOPE,
        "now_ms": FRIDAY_MS,
    }
    kwargs.update(overrides)
    return arming_status(records, **kwargs)


def test_the_record_seals_every_field_and_round_trips() -> None:
    record = _armed()
    assert record.kind == "armed" and record.schema_version == 1
    assert len(record.record_sha256) == 64
    assert record.envelope_sha256 == ENVELOPE.sha
    assert record.envelope == ENVELOPE
    assert LiveArmingRecord.from_payload(asdict(record)) == record


def test_a_successor_record_seals_its_shadow_predecessor_and_round_trips() -> None:
    predecessor = RehearsalPredecessor(
        strategy_instance_id="ema-shadow-rehearsal",
        seal_hash="d" * 64,
        receipt_sha256="e" * 64,
    )
    record = LiveArmingRecord.create(
        live_account_id=ACCOUNT,
        strategy_instance_id="ema-live-successor",
        seal_hash=SEAL,
        configured_signal_hash=SIGNAL,
        shadow_receipt_sha256=None,
        envelope=ENVELOPE,
        armed_at_ms=FRIDAY_MS,
        max_sessions=ENVELOPE.arming_max_sessions,
        predecessor=predecessor,
        originating_plan_id="f" * 64,
    )

    assert record.schema_version == 2
    assert record.predecessor == predecessor
    assert LiveArmingRecord.from_payload(asdict(record)) == record

    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveArmingRecord.from_payload(
            {**asdict(record), "predecessor": {**asdict(predecessor), "seal_hash": "f" * 64}}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("live_account_id", "9LIVE0002"),
        ("strategy_instance_id", "other"),
        ("seal_hash", "d" * 64),
        ("configured_signal_hash", "d" * 64),
        ("shadow_receipt_sha256", "d" * 64),
        ("armed_at_ms", FRIDAY_MS + 1),
        ("max_sessions", 19),
        ("kind", "disarmed"),
        ("schema_version", 2),
        ("envelope_sha256", "d" * 64),
    ],
)
def test_tampering_with_any_sealed_field_is_refused(field: str, value: object) -> None:
    """Whichever check fires first, no hand-edited row is ever accepted."""
    payload = {**asdict(_armed()), field: value}
    with pytest.raises(LiveArmingInvalid):
        LiveArmingRecord.from_payload(payload)


def test_a_tampered_content_field_is_caught_by_the_record_digest() -> None:
    """The digest is what catches an edit the per-field validators would allow.

    ``armed_at_ms`` is such a field: one millisecond later is still a valid
    instant, cross-checked against nothing else on the row.
    """
    payload = {**asdict(_armed()), "armed_at_ms": FRIDAY_MS + 1}
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveArmingRecord.from_payload(payload)


def test_a_wrong_kind_or_schema_version_is_named_as_such() -> None:
    """A row of the wrong shape is not an "integer or identity" problem."""
    record = _armed()
    with pytest.raises(LiveArmingInvalid, match="invalid kind or schema version"):
        LiveArmingRecord.from_payload({**asdict(record), "kind": "disarmed"})
    with pytest.raises(LiveArmingInvalid, match="invalid kind or schema version"):
        LiveArmingRecord.from_payload({**asdict(record), "schema_version": 2})
    disarm = _disarmed(record)
    with pytest.raises(LiveArmingInvalid, match="invalid kind or schema version"):
        LiveDisarmRecord.from_payload({**asdict(disarm), "kind": "armed"})


def test_a_rewritten_envelope_is_caught_by_the_envelope_sha_before_the_digest() -> None:
    """R1's second check: the sealed values must still hash to the sealed sha."""
    record = _armed()
    payload = {**asdict(record), "envelope_values": {**record.envelope_values, "loss_usd": 4_000.0}}
    with pytest.raises(LiveArmingInvalid, match="envelope sha does not match"):
        LiveArmingRecord.from_payload(payload)


def test_the_lapse_count_must_be_the_sealed_envelopes_own() -> None:
    """``max_sessions`` is not a second, independent number.

    ``arming_status`` counts the lapse off the record's own ``max_sessions``
    while the envelope sealed beside it -- the one the operator confirmed, and
    the one the sync publishes -- carries ``arming_max_sessions``. Nothing but
    this check forces them to agree, so a record whose sealed envelope says 20
    sessions could otherwise stay armed for 100.
    """
    with pytest.raises(LiveArmingInvalid, match="disagrees with the sealed envelope"):
        _armed(max_sessions=ENVELOPE.arming_max_sessions + 1, envelope=ENVELOPE)


def test_a_resealed_row_whose_lapse_count_was_widened_is_still_refused() -> None:
    """The digest cannot catch this one: the row was re-sealed over the new number."""
    unsigned = {name: value for name, value in asdict(_armed()).items() if name != "record_sha256"}
    widened = {**unsigned, "max_sessions": 100}
    resealed = {**widened, "record_sha256": canonical_sha256(widened)}

    with pytest.raises(LiveArmingInvalid, match="disagrees with the sealed envelope"):
        LiveArmingRecord.from_payload(resealed)


def test_a_reserved_namespace_account_is_never_a_live_account() -> None:
    with pytest.raises(LiveArmingInvalid, match="reserved account identity"):
        _armed(account=f"shadow:{ACCOUNT}")


def test_invalid_integer_and_hash_facts_are_refused() -> None:
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(max_sessions=0)
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(armed_at_ms=MAX_TIMESTAMP_MS + 1)
    assert _armed(armed_at_ms=MAX_TIMESTAMP_MS).armed_at_ms == MAX_TIMESTAMP_MS
    with pytest.raises(LiveArmingInvalid, match="invalid hash facts"):
        _armed(seal="not-a-sha")


@pytest.mark.parametrize("value", [1.0, True])
def test_an_integer_fact_that_is_a_float_or_a_bool_is_refused(value: object) -> None:
    """A dataclass does not enforce its annotations, and ``True`` is an ``int``.

    A row whose digest was recomputed over the tampered value verifies, so the
    range checks are the only thing between ``1.0`` / ``True`` and the calendar
    conversion that reads ``armed_at_ms`` as ``int64 ms UTC``. Both compare
    fine against a bound, so the type has to be asked for by name.
    """
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(max_sessions=value)  # type: ignore[arg-type]
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _armed(armed_at_ms=value)  # type: ignore[arg-type]
    with pytest.raises(LiveArmingInvalid, match="invalid kind or schema version"):
        LiveArmingRecord.from_payload({**asdict(_armed()), "schema_version": value})

    record = _armed()
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        _disarmed(record, at_ms=value)  # type: ignore[arg-type]
    with pytest.raises(LiveArmingInvalid, match="invalid kind or schema version"):
        LiveDisarmRecord.from_payload({**asdict(_disarmed(record)), "schema_version": value})


@pytest.mark.parametrize("key", ["shadow_sessions", "arming_max_sessions"])
@pytest.mark.parametrize("value", [1.0, True])
def test_a_resealed_envelope_integer_that_is_a_float_or_a_bool_is_refused(key: str, value: object) -> None:
    """``LiveEnvelopeValues`` is a plain dataclass with no field validation of its
    own, unlike the record's own four integers. A row re-sealed over a
    tampered ``envelope_values`` entry verifies both its own digest and its
    envelope sha, so only a type check by name catches ``1.0`` or ``True``
    where ``shadow_sessions`` or ``arming_max_sessions`` promises an integer.
    """
    record = _armed()
    tampered_envelope = {**record.envelope_values, key: value}
    unsigned = {name: field_value for name, field_value in asdict(record).items() if name != "record_sha256"}
    widened = {
        **unsigned,
        "envelope_values": tampered_envelope,
        "envelope_sha256": canonical_sha256(tampered_envelope),
    }
    resealed = {**widened, "record_sha256": canonical_sha256(widened)}

    with pytest.raises(LiveArmingInvalid, match="invalid integer facts"):
        LiveArmingRecord.from_payload(resealed)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("loss_fraction", 0.0),
        ("loss_fraction", 1.0),
        ("loss_usd", 0.0),
        # ``inf`` satisfies ``> 0`` and makes every loss comparison False, so a
        # sealed infinity would be a loss limit nothing can ever breach.
        ("loss_usd", float("inf")),
        ("loss_usd", float("nan")),
        ("shadow_sessions", 0),
        ("xh_entry_bps", -1.0),
        # 10 000 bps is 100 %: a sell anchored there floors to zero, and the
        # leg is refused EXTENDED_ANCHOR_UNPRICEABLE -- an EXIT refused
        # *because of* a seal, which the envelope rule forbids.
        ("xh_exit_bps", 10_000.0),
    ],
)
def test_a_resealed_envelope_float_outside_its_domain_is_refused(key: str, value: float) -> None:
    """The sealed floats bound real money, so they carry the environment's domains.

    ``AlpacaSettings`` enforces these on ``ALPACA_LIVE_*`` and refuses to boot
    outside them. The sealed copy is what the loss hold is judged against and
    what an extended-session leg is priced from, and it reaches
    ``LiveEnvelopeValues`` -- a plain dataclass with no validation of its own --
    straight off disk. A row re-sealed over the tampered value verifies both
    digests, so only this check stands between it and the money.

    The domains themselves are pinned to the settings that declare them by
    ``tests/broker/alpaca/test_config.py::
    test_the_envelope_domains_agree_with_the_settings_that_declare_them``.
    """
    record = _armed()
    tampered_envelope = {**record.envelope_values, key: value}
    unsigned = {name: field_value for name, field_value in asdict(record).items() if name != "record_sha256"}
    widened = {
        **unsigned,
        "envelope_values": tampered_envelope,
        "envelope_sha256": canonical_sha256(tampered_envelope),
    }
    resealed = {**widened, "record_sha256": canonical_sha256(widened)}

    with pytest.raises(LiveArmingInvalid, match=f"sealed {key} is not"):
        LiveArmingRecord.from_payload(resealed)


def test_a_type_confused_row_leaves_by_this_modules_own_error() -> None:
    payload = {**asdict(_armed()), "armed_at_ms": str(FRIDAY_MS)}
    with pytest.raises(LiveArmingInvalid, match="invalid integer or identity facts"):
        LiveArmingRecord.from_payload(payload)
    with pytest.raises(LiveArmingInvalid, match="invalid shape"):
        LiveArmingRecord.from_payload({"kind": "armed"})


def test_a_disarm_row_is_sealed_and_names_what_it_revokes() -> None:
    record = _armed()
    disarm = _disarmed(record)
    assert disarm.kind == "disarmed" and disarm.revokes_record_sha256 == record.record_sha256
    assert LiveDisarmRecord.from_payload(asdict(disarm)) == disarm
    with pytest.raises(LiveArmingInvalid, match="digest does not verify"):
        LiveDisarmRecord.from_payload({**asdict(disarm), "disarmed_at_ms": MONDAY_MS + 1})


def test_a_refusal_carries_its_reason_code() -> None:
    refusal = LiveArmingRefused(LIVE_ARMING_LAPSED, "the arming lapsed")
    assert refusal.reason_code == LIVE_ARMING_LAPSED
    assert str(refusal) == "the arming lapsed"
    assert isinstance(refusal, ValueError)


def test_every_code_is_its_own_name_and_the_set_is_closed() -> None:
    assert frozenset(
        {
            "LIVE_ARMING_LAPSED",
            "LIVE_ARMING_REVOKED",
            "LIVE_ARMING_SEAL_CHANGED",
            "LIVE_ARMING_INSTANCE_UNSEALED",
            "LIVE_ARMING_TOKEN_INVALID",
            "LIVE_ARMING_PLAN_EXPIRED",
            "LIVE_ARMING_INPUTS_CHANGED",
            "LIVE_ARMING_NOT_ARMED",
            "LIVE_ARMING_TTL_INVALID",
            "LIVE_ARMING_FUTURE_DATED",
            "LIVE_SHADOW_INCOMPLETE",
            "LIVE_ENVELOPE_DISAGREEMENT",
            "LIVE_ENVELOPE_MISSING",
            "LIVE_ARMING_REQUIRED",
            "LIVE_ARMING_UNOBSERVED",
            "LIVE_ARMING_LEDGER_INVALID",
            "LIVE_VERDICT_TRANSITION_HALT",
        }
    ) == ARMING_REASON_CODES
    assert LIVE_ENVELOPE_MISSING == "LIVE_ENVELOPE_MISSING"
    assert LIVE_ARMING_FUTURE_DATED == "LIVE_ARMING_FUTURE_DATED"
    from app.broker.alpaca.clerk import live_arming as live_arming_module

    for name in ARMING_REASON_CODES:
        assert getattr(live_arming_module, name) == name


def test_the_arming_session_counts_as_one_and_a_weekend_spends_nothing() -> None:
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS) == 1
    # Saturday and Sunday are not sessions: Monday is only the second.
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == 2
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=TUESDAY_MS) == 3
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=MONDAY_MS) == trading_session_count(
        date(2026, 9, 11), date(2026, 9, 14)
    )


def test_a_market_holiday_spends_nothing_either() -> None:
    """Thanksgiving 2026-11-26 is a Thursday and not a session; 11-27 is a half day."""
    assert not is_trading_day(date(2026, 11, 26))
    armed = et_minute_of_day_ms(date(2026, 11, 25), 10 * 60)
    checked = et_minute_of_day_ms(date(2026, 11, 30), 10 * 60)
    expected = trading_session_count(date(2026, 11, 25), date(2026, 11, 30))
    assert expected == 3  # Wed 25, Fri 27 (half day), Mon 30
    assert sessions_used(armed_at_ms=armed, now_ms=checked) == expected


def test_sessions_used_refuses_a_reversed_range() -> None:
    """A now_ms behind the arming fails closed, to the millisecond -- the calendar never sees a reversed range."""
    with pytest.raises(ValueError, match="precedes armed_at_ms"):
        sessions_used(armed_at_ms=MONDAY_MS, now_ms=FRIDAY_MS)
    with pytest.raises(ValueError):
        sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS - 1)
    assert sessions_used(armed_at_ms=FRIDAY_MS, now_ms=FRIDAY_MS) == 1


def test_no_record_is_unarmed() -> None:
    assert _status([]) == ArmingStatus(
        state="unarmed", reason_code=None, record=None, sessions_used=0, sessions_remaining=0
    )


def test_the_latest_record_decides_and_a_re_arm_supersedes() -> None:
    first = _armed(max_sessions=2)
    second = _armed(max_sessions=5, armed_at_ms=MONDAY_MS)
    status = _status([first, second], now_ms=MONDAY_MS, configured_envelope=second.envelope)
    assert (status.state, status.record) == ("armed", second)
    assert (status.sessions_used, status.sessions_remaining) == (1, 4)


def test_a_disarm_revokes_until_the_instance_is_armed_again() -> None:
    record = _armed()
    status = _status([record, _disarmed(record)], now_ms=MONDAY_MS)
    assert (status.state, status.reason_code, status.record) == ("disarmed", LIVE_ARMING_REVOKED, None)
    assert (status.sessions_used, status.sessions_remaining) == (0, 0)

    rearmed = _armed(armed_at_ms=MONDAY_MS)
    assert _status([record, _disarmed(record), rearmed], now_ms=MONDAY_MS).state == "armed"


def test_a_changed_seal_disarms_and_an_absent_binding_counts_as_changed() -> None:
    records = [_armed()]
    changed = _status(records, seal_hash="d" * 64)
    assert (changed.state, changed.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    absent = _status(records, seal_hash=None)
    assert (absent.state, absent.reason_code) == ("disarmed", LIVE_ARMING_SEAL_CHANGED)
    # The record is still reported so an operator can see what was armed.
    assert changed.record == records[0] and changed.sessions_used == 1


def test_a_changed_environment_disagrees_with_the_sealed_envelope() -> None:
    status = _status([_armed()], configured_envelope=replace(ENVELOPE, loss_usd=4_000.0))
    assert (status.state, status.reason_code) == ("disarmed", LIVE_ENVELOPE_DISAGREEMENT)


def test_the_arming_lapses_only_once_the_count_is_exceeded() -> None:
    records = [_armed(max_sessions=2)]
    granted = records[0].envelope
    on_the_last_session = _status(records, now_ms=MONDAY_MS, configured_envelope=granted)
    assert on_the_last_session.state == "armed"
    assert (on_the_last_session.sessions_used, on_the_last_session.sessions_remaining) == (2, 0)

    lapsed = _status(records, now_ms=TUESDAY_MS, configured_envelope=granted)
    assert (lapsed.state, lapsed.reason_code) == ("lapsed", LIVE_ARMING_LAPSED)
    assert (lapsed.sessions_used, lapsed.sessions_remaining) == (3, 0)


def test_a_record_dated_after_the_clock_is_disarmed() -> None:
    """A record armed after now_ms fails closed under a thirteenth code (controller ruling)."""
    record = _armed(armed_at_ms=MONDAY_MS)
    status = _status([record], now_ms=FRIDAY_MS)
    assert (status.state, status.reason_code, status.record) == ("disarmed", LIVE_ARMING_FUTURE_DATED, record)
    assert (status.sessions_used, status.sessions_remaining) == (0, record.max_sessions)


def test_the_checks_run_in_the_order_r5_fixes() -> None:
    """A record that fails three ways at once is reported by the first failure."""
    records = [_armed(max_sessions=1)]
    status = _status(
        records, seal_hash="d" * 64, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS
    )
    assert status.reason_code == LIVE_ARMING_SEAL_CHANGED
    envelope_first = _status(records, configured_envelope=replace(ENVELOPE, loss_usd=4_000.0), now_ms=TUESDAY_MS)
    assert envelope_first.reason_code == LIVE_ENVELOPE_DISAGREEMENT


def test_another_accounts_or_another_instances_record_never_answers_here() -> None:
    foreign_account = _armed(account="9LIVE0002")
    foreign_instance = _armed(instance="other")
    assert _status([foreign_account, foreign_instance]).state == "unarmed"


def test_a_record_without_a_receipt_seals_round_trips_and_still_detects_tampering() -> None:
    """Shadow is a mode, not a requirement (owner decision 2026-09-09)."""
    record = LiveArmingRecord.create(
        live_account_id=ACCOUNT,
        strategy_instance_id=SID,
        seal_hash=SEAL,
        configured_signal_hash=SIGNAL,
        shadow_receipt_sha256=None,
        envelope=ENVELOPE,
        armed_at_ms=FRIDAY_MS,
        max_sessions=ENVELOPE.arming_max_sessions,
    )
    payload = asdict(record)
    assert payload["shadow_receipt_sha256"] is None
    assert LiveArmingRecord.from_payload(payload) == record
    with pytest.raises(LiveArmingInvalid):
        LiveArmingRecord.from_payload({**payload, "shadow_receipt_sha256": "e" * 64})
    with pytest.raises(LiveArmingInvalid):
        LiveArmingRecord.from_payload({**payload, "seal_hash": "f" * 64})
    # A receipt-less record still grants exactly what an armed row grants.
    status = arming_status(
        (record,),
        live_account_id=ACCOUNT,
        strategy_instance_id=SID,
        seal_hash=SEAL,
        configured_envelope=ENVELOPE,
        now_ms=FRIDAY_MS,
    )
    assert status.state == "armed"
