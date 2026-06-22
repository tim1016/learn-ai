"""Tests for ``app.schemas.broker_activity`` — ADR 0014 row contract.

Covers: enum cardinality (closed sets), row/page round-trip, and the
``ReconciliationTimingPolicy`` ordering invariant. Template rendering and
reconciliation logic live in their own test modules; this file is the
schema-shape contract only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.broker_activity import (
    BrokerActivityPage,
    BrokerActivityRow,
    DivergenceFacts,
    EngineOverlay,
    LagBreakdown,
    ReasonCode,
    ReconciliationTimingPolicy,
    SizingProvenance,
    Verdict,
)

# ── Verdict / ReasonCode closed cardinality ────────────────────────────


def test_verdict_has_exactly_four_values() -> None:
    """ADR 0014 §2 — the four-value verdict enum is intentionally
    closed. Adding a fifth would force the frontend to grow a new chip
    color path AND violate the truthfulness contract (forensic detail
    belongs in reason_codes, not extra verdicts). Lock the cardinality."""
    assert {v.value for v in Verdict} == {
        "expected",
        "expected_with_caveat",
        "unexpected",
        "engine_only_pending",
    }


def test_reason_code_vocabulary_is_complete_for_v1_templates() -> None:
    """ADR 0014 §3 — the closed reason-code vocabulary drives template
    selection. Locking this set means a new reason requires a code change
    AND a matching template (caught by template-selection tests).
    """
    assert {r.value for r in ReasonCode} == {
        "normal_fill",
        "pending_acknowledgement",
        "partial_fill",
        "timing_caveat",
        "reconnect_recovery",
        "missing_commission",
        "price_divergence",
        "quantity_divergence",
        "unmatched_execution",
        "duplicate_execution",
        "cancellation",
        "rejection",
    }


# ── BrokerActivityRow round-trip ───────────────────────────────────────


def _minimal_row(**overrides) -> BrokerActivityRow:
    base = {
        "seq": 1,
        "ts_ms": 1_700_000_000_000,
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 100.0,
        "order_type": "MKT",
        "verdict": Verdict.EXPECTED,
        "template_key": "normal_fill",
        "template_version": 1,
        "headline": "Filled 100 SPY at $450.00",
        "narrative": "Market order filled in full at $450.00; $1.00 commission",
    }
    base.update(overrides)
    return BrokerActivityRow(**base)


def test_row_round_trips_through_json() -> None:
    row = _minimal_row(
        exec_id="exec-1",
        perm_id=12345,
        order_ref="learn-ai/sid-a/v1:intent-x",
        price=450.0,
        commission=1.0,
        net_amount=-45_001.0,
        exec_ts_ms=1_700_000_000_500,
        engine_overlay=EngineOverlay(
            intent_id="intent-x",
            requested_qty=100.0,
            sizing_provenance=SizingProvenance(policy="SetHoldings"),
            lag_breakdown=LagBreakdown(intent_to_exec_ms=420),
        ),
    )

    rt = BrokerActivityRow.model_validate_json(row.model_dump_json())
    assert rt == row


def test_row_rejects_unknown_fields() -> None:
    """``extra='forbid'`` is the truthfulness contract — a future field
    must be modeled, not appear ad-hoc on the wire."""
    with pytest.raises(ValidationError):
        BrokerActivityRow.model_validate(
            {
                "seq": 1,
                "ts_ms": 1_700_000_000_000,
                "symbol": "SPY",
                "side": "BUY",
                "quantity": 100.0,
                "order_type": "MKT",
                "verdict": "expected",
                "template_key": "normal_fill",
                "template_version": 1,
                "headline": "x",
                "narrative": "y",
                "mystery_field": "should reject",
            }
        )


def test_row_requires_positive_ts_ms_and_nonneg_seq() -> None:
    with pytest.raises(ValidationError):
        _minimal_row(ts_ms=0)
    with pytest.raises(ValidationError):
        _minimal_row(seq=-1)


def test_engine_only_pending_row_has_no_exec_identity() -> None:
    """A ``ENGINE_ONLY_PENDING`` row predates the broker ack — it has
    no exec_id / perm_id / exec_ts_ms. The schema does not enforce this
    business rule (the publisher does) but the optional shape allows it."""
    row = _minimal_row(
        verdict=Verdict.ENGINE_ONLY_PENDING,
        template_key="pending_acknowledgement",
        headline="Pending broker acknowledgement",
        narrative="Intent submitted; awaiting broker ack",
    )
    assert row.exec_id is None
    assert row.perm_id is None
    assert row.exec_ts_ms is None


# ── BrokerActivityPage shape ───────────────────────────────────────────


def test_page_with_no_next_seq_indicates_drained_wal() -> None:
    page = BrokerActivityPage(rows=[_minimal_row()], next_seq=None)
    assert page.next_seq is None


def test_page_with_next_seq_indicates_more_to_fetch() -> None:
    page = BrokerActivityPage(rows=[_minimal_row()], next_seq=42)
    assert page.next_seq == 42


# ── ReconciliationTimingPolicy ─────────────────────────────────────────


def test_timing_policy_defaults_are_conservative() -> None:
    """Defaults must keep production traffic in ``EXPECTED`` for typical
    paper-trading lag (sub-second). If these change, the runbook needs
    a corresponding update."""
    policy = ReconciliationTimingPolicy()
    assert policy.caveat_lag_ms == 2_000
    assert policy.excessive_lag_ms == 10_000


def test_timing_policy_rejects_excessive_le_caveat() -> None:
    """ADR 0014 §6 — excessive must be strictly greater than caveat.
    A policy with the two equal would collapse two verdict tiers into
    one and silently route ``UNEXPECTED`` cases through the caveat
    template."""
    with pytest.raises(ValidationError) as exc_info:
        ReconciliationTimingPolicy(caveat_lag_ms=1_000, excessive_lag_ms=1_000)
    assert "excessive_lag_ms" in str(exc_info.value)

    with pytest.raises(ValidationError):
        ReconciliationTimingPolicy(caveat_lag_ms=5_000, excessive_lag_ms=1_000)


def test_timing_policy_rejects_nonpositive_thresholds() -> None:
    with pytest.raises(ValidationError):
        ReconciliationTimingPolicy(caveat_lag_ms=0, excessive_lag_ms=1_000)
    with pytest.raises(ValidationError):
        ReconciliationTimingPolicy(caveat_lag_ms=-1, excessive_lag_ms=1_000)


# ── Drill-down shapes ──────────────────────────────────────────────────


def test_divergence_facts_accepts_typed_window_context() -> None:
    facts = DivergenceFacts(
        price_delta=0.02,
        lag_total_ms=8_500,
        window_context={
            "reconnect_window_start_ms": 1_700_000_000_000,
            "reconnect_window_end_ms": 1_700_000_008_000,
            "label": "ibkr-reconnect-3",
        },
    )
    assert facts.window_context["label"] == "ibkr-reconnect-3"


def test_lag_breakdown_all_phases_optional() -> None:
    """A foreign exec has no matching engine intent, so all phase
    timestamps are absent — every phase must be ``None``-able. The
    chip's ``intent_to_exec_ms`` is the only field the frontend reads."""
    lag = LagBreakdown()
    assert lag.intent_to_exec_ms is None
    assert lag.exec_to_observed_ms is None
