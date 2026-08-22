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


def _receipts(stations) -> dict[str, str]:
    return {s.station_id: s.receipt for s in stations}


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
        decisions=[
            decision_receipt(
                seq=1,
                ts_ms=_NOW - 500,
                outcome="entered",
                reason_code="CROSS_UP",
                order_ref=ref,
            )
        ],
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
        decisions=[],
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
        decisions=[],
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
        decisions=[
            decision_receipt(
                seq=1,
                ts_ms=_NOW - STALE_THRESHOLD_MS - 1,
                outcome="no_action",
                reason_code="FLAT",
            )
        ],
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
        decisions=[],
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
        decisions=[],
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "unknown_stale"


def test_cross_bot_transaction_ref_blocks_order_stations() -> None:
    """A ref from another bot's namespace must block every transaction-scoped
    station, SIGNAL included (#1729 AC #6/#7) — this bot's own unrelated
    decision must never render as though it belonged to a foreign transaction."""
    other_ref = order_ref(OTHER_SID, "i1")
    entries = [intent_entry(sid=OTHER_SID, intent="i1", ts_ms=_NOW - 1000)]
    stations = derive_stations(
        sid=SID,
        transaction_ref=other_ref,
        all_entries=entries,
        decisions=[
            decision_receipt(seq=1, ts_ms=_NOW - 500, outcome="no_action", reason_code="FLAT")
        ],
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["INTENT"] == "blocked"
    assert states["SUBMIT_GATE"] == "blocked"
    assert states["BROKER_ACK"] == "blocked"
    assert states["FILL"] == "blocked"
    assert states["SIGNAL"] == "blocked"


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
        decisions=[],
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
        decisions=[],
        latest_reconciliation=recon,
        now_ms=_NOW,
    )
    states = _states(stations)
    assert states["RECONCILED"] == "satisfied"


def test_signal_shows_the_decision_linked_to_the_selected_transaction() -> None:
    """#1729 AC #6/#7 regression: SIGNAL must reflect the *selected*
    transaction's own decision, matched by order_ref, not simply the bot's
    most recently recorded decision."""
    old_ref = order_ref(SID, "i1")
    new_ref = order_ref(SID, "i2")
    decisions = [
        decision_receipt(
            seq=1, ts_ms=_NOW - 5_000, outcome="entered", reason_code="CROSS_UP", order_ref=old_ref
        ),
        decision_receipt(
            seq=2, ts_ms=_NOW - 100, outcome="exited", reason_code="CROSS_DOWN", order_ref=new_ref
        ),
    ]

    stations_for_old = derive_stations(
        sid=SID,
        transaction_ref=old_ref,
        all_entries=[],
        decisions=decisions,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    receipts = _receipts(stations_for_old)
    assert "CROSS_UP" in receipts["SIGNAL"]
    assert "CROSS_DOWN" not in receipts["SIGNAL"]

    stations_for_new = derive_stations(
        sid=SID,
        transaction_ref=new_ref,
        all_entries=[],
        decisions=decisions,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    receipts_new = _receipts(stations_for_new)
    assert "CROSS_DOWN" in receipts_new["SIGNAL"]


def test_signal_matches_by_intent_id_when_order_ref_does_not_match() -> None:
    """A decision recorded against the transaction's intent id (rather than
    its order_ref) still resolves — mirrors
    ``decision_receipts_by_transaction``'s ``intent_id = ? OR order_ref = ?``
    join, which this function's in-memory match is required to stay
    equivalent to."""
    ref = order_ref(SID, "i1")
    decisions = [
        decision_receipt(
            seq=1,
            ts_ms=_NOW - 500,
            outcome="enter_intent",
            reason_code="CROSS_UP",
            intent_id=ref,
        )
    ]
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=[],
        decisions=decisions,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    assert _states(stations)["SIGNAL"] == "satisfied"


def test_signal_is_explicit_absence_when_no_decision_is_linked_to_the_selected_transaction() -> None:
    """A transaction with no causally-linked decision in the retained window
    must render explicit absence, never a substituted unrelated decision."""
    ref = order_ref(SID, "i1")
    unrelated_ref = order_ref(SID, "i2")
    decisions = [
        decision_receipt(
            seq=1,
            ts_ms=_NOW - 100,
            outcome="no_action",
            reason_code="FLAT",
            order_ref=unrelated_ref,
        )
    ]
    stations = derive_stations(
        sid=SID,
        transaction_ref=ref,
        all_entries=[],
        decisions=decisions,
        latest_reconciliation=None,
        now_ms=_NOW,
    )
    states = _states(stations)
    receipts = _receipts(stations)
    assert states["SIGNAL"] == "not_applicable"
    assert ref in receipts["SIGNAL"]
