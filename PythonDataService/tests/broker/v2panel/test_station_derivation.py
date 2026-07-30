"""Tests for the six-station transaction-rail derivation (S1, spec §7.1)."""

from __future__ import annotations

from app.broker.v2panel.vocabulary import STATION_IDS
from app.services.broker_v2_panel.station_derivation import (
    STALE_THRESHOLD_MS,
    derive_stations,
    transaction_refs_for_bot,
)
from tests.broker.v2panel.fixtures import (
    OTHER_SID,
    SID,
    decision_receipt,
    fill_entry,
    intent_entry,
    order_ref,
    reconciliation_entry,
    submit_acked_entry,
    submit_failed_entry,
)

_NOW = 1_700_000_000_000


def _states(stations) -> dict[str, str]:
    return {s.station_id: s.state for s in stations}


def test_transaction_refs_only_target_bot() -> None:
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=1000),
        intent_entry(sid="other", intent="i2", ts_ms=2000),
    ]
    refs = transaction_refs_for_bot(SID, entries)
    assert refs == [order_ref(SID, "i1")]


def test_full_lifecycle_all_stations_satisfied() -> None:
    ref = order_ref(SID, "i1")
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=_NOW - 1000),
        submit_acked_entry(sid=SID, intent="i1", ts_ms=_NOW - 900),
        fill_entry(sid=SID, intent="i1", ts_ms=_NOW - 800),
        reconciliation_entry(verdict="clean", ts_ms=_NOW - 700),
    ]
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=entries,
        latest_decision=decision_receipt(
            seq=1, ts_ms=_NOW - 500, outcome="entered", reason_code="CROSS_UP"
        ),
        latest_reconciliation=entries[-1],
        now_ms=_NOW,
    )
    states = _states(stations)
    assert [s.station_id for s in stations] == list(STATION_IDS)
    assert states == {
        "SIGNAL": "satisfied",
        "INTENT": "satisfied",
        "SUBMIT_GATE": "satisfied",
        "BROKER_ACK": "satisfied",
        "FILL": "satisfied",
        "RECONCILED": "satisfied",
    }


def test_submit_failed_blocks_broker_ack() -> None:
    ref = order_ref(SID, "i1")
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=_NOW - 1000),
        submit_failed_entry(sid=SID, intent="i1", ts_ms=_NOW - 900),
    ]
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=entries,
        latest_decision=None,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["BROKER_ACK"] == "blocked"
    assert states["FILL"] == "waiting"


def test_no_transaction_leaves_order_stations_waiting() -> None:
    stations = derive_stations(
        sid=SID,
        transaction_ref=None,
        all_entries=[],
        latest_decision=None,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["INTENT"] == "waiting"
    assert states["SUBMIT_GATE"] == "waiting"
    assert states["FILL"] == "waiting"


def test_stale_decision_downgrades_signal_to_unknown_stale() -> None:
    stations = derive_stations(
        sid=SID,
        transaction_ref=None,
        all_entries=[],
        latest_decision=decision_receipt(
            seq=1,
            ts_ms=_NOW - STALE_THRESHOLD_MS - 1,
            outcome="no_action",
            reason_code="FLAT",
        ),
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["SIGNAL"] == "unknown_stale"


def test_unexplained_order_verdict_blocks_reconciled() -> None:
    recon = reconciliation_entry(verdict="unexplained_order", ts_ms=_NOW - 100)
    stations = derive_stations(
        sid=SID,
        transaction_ref=None,
        all_entries=[recon],
        latest_decision=None,
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "blocked"


def test_stale_reconciliation_verdict_is_unknown_stale() -> None:
    recon = reconciliation_entry(verdict="stale", ts_ms=_NOW - 100)
    stations = derive_stations(
        sid=SID,
        transaction_ref=None,
        all_entries=[recon],
        latest_decision=None,
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "unknown_stale"


def test_cross_bot_transaction_ref_blocks_order_stations() -> None:
    """A ref from another bot's namespace must block the four order-scoped stations."""
    other_ref = order_ref(OTHER_SID, "i1")
    entries = [intent_entry(sid=OTHER_SID, intent="i1", ts_ms=_NOW - 1000)]
    stations = derive_stations(
        sid=SID,
        transaction_ref=other_ref,
        all_entries=entries,
        latest_decision=None,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["INTENT"] == "blocked"
    assert states["SUBMIT_GATE"] == "blocked"
    assert states["BROKER_ACK"] == "blocked"
    assert states["FILL"] == "blocked"
    # SIGNAL is bot-scoped, not order-scoped — unaffected.
    assert states["SIGNAL"] == "waiting"


def test_causal_reconciliation_gate_blocks_when_sweep_predates_submit() -> None:
    """A sweep before the SUBMIT_ACKED entry must be blocked as stale evidence."""
    submit_ts = _NOW - 500
    sweep_ts = _NOW - 600  # predates the submission
    ref = order_ref(SID, "i1")
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=_NOW - 1000),
        submit_acked_entry(sid=SID, intent="i1", ts_ms=submit_ts),
    ]
    recon = reconciliation_entry(verdict="clean", ts_ms=sweep_ts)
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=entries,
        latest_decision=None,
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "blocked"


def test_causal_reconciliation_gate_passes_when_sweep_postdates_submit() -> None:
    """A sweep after the SUBMIT_ACKED entry is valid evidence."""
    submit_ts = _NOW - 500
    sweep_ts = _NOW - 400  # postdates the submission
    ref = order_ref(SID, "i1")
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=_NOW - 1000),
        submit_acked_entry(sid=SID, intent="i1", ts_ms=submit_ts),
    ]
    recon = reconciliation_entry(verdict="clean", ts_ms=sweep_ts)
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=entries,
        latest_decision=None,
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "satisfied"
