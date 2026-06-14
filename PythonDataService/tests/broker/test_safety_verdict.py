"""Phase 7A / VCR-0010 / ADR 0011 — broker safety verdict derivation."""

from __future__ import annotations

import pytest

from app.broker.safety_verdict import (
    BrokerSafetyVerdict,
    classify_account_prefix,
    classify_port,
    derive_broker_safety_verdict,
)


def test_classify_port_paper_ports_recognized() -> None:
    assert classify_port(7497) == "paper_port"
    assert classify_port(4002) == "paper_port"


def test_classify_port_live_ports_recognized() -> None:
    assert classify_port(7496) == "live_port"
    assert classify_port(4001) == "live_port"


def test_classify_port_unknown_for_unrecognized() -> None:
    """Unknown ports degrade to ``unknown``, never to ``paper_port`` — a
    misconfigured port cannot silently make a live session look safe."""
    assert classify_port(8080) == "unknown"
    assert classify_port(None) == "unknown"


def test_classify_account_prefix_du_is_paper() -> None:
    assert classify_account_prefix("DU1234567") == "DU"
    assert classify_account_prefix("du1234567") == "DU"


def test_classify_account_prefix_non_du_is_live() -> None:
    assert classify_account_prefix("U1234567") == "non_DU"


def test_classify_account_prefix_none_for_empty() -> None:
    assert classify_account_prefix(None) is None
    assert classify_account_prefix("") is None


def test_derive_paper_only_when_every_gate_confirms() -> None:
    """The strictest case — every gate must positively confirm paper.
    Any missing or contradicting gate degrades the verdict."""
    verdict = derive_broker_safety_verdict(
        configured_mode="paper",
        readonly_flag=True,
        port=7497,
        connected_account="DU1234567",
    )
    assert verdict.final_verdict == "paper-only"
    assert verdict.failing_gates == []
    assert verdict.unknown_gates == []


def test_derive_unsafe_when_mode_is_live() -> None:
    verdict = derive_broker_safety_verdict(
        configured_mode="live",
        readonly_flag=True,
        port=7497,
        connected_account="DU1234567",
    )
    assert verdict.final_verdict == "unsafe"
    assert "configured_mode" in verdict.failing_gates


def test_derive_unsafe_when_port_is_live() -> None:
    verdict = derive_broker_safety_verdict(
        configured_mode="paper",
        readonly_flag=True,
        port=7496,
        connected_account="DU1234567",
    )
    assert verdict.final_verdict == "unsafe"
    assert "port_class" in verdict.failing_gates


def test_derive_unsafe_when_account_is_non_du() -> None:
    verdict = derive_broker_safety_verdict(
        configured_mode="paper",
        readonly_flag=True,
        port=7497,
        connected_account="U1234567",
    )
    assert verdict.final_verdict == "unsafe"
    assert "connected_account_prefix" in verdict.failing_gates


def test_derive_unknown_when_account_missing() -> None:
    """Pre-connect / disconnected state: account_id absent → unknown."""
    verdict = derive_broker_safety_verdict(
        configured_mode="paper",
        readonly_flag=True,
        port=7497,
        connected_account=None,
    )
    assert verdict.final_verdict == "unknown"
    assert "connected_account_prefix" in verdict.unknown_gates


def test_derive_unknown_when_readonly_unverified() -> None:
    """The readonly gate must positively confirm True. None or False both
    degrade to unknown (never paper-only) so the trust anchor stays honest."""
    for ro in (None, False):
        verdict = derive_broker_safety_verdict(
            configured_mode="paper",
            readonly_flag=ro,
            port=7497,
            connected_account="DU1234567",
        )
        assert verdict.final_verdict == "unknown"
        assert "readonly_flag" in verdict.unknown_gates


def test_unsafe_dominates_unknown() -> None:
    """If any gate is positively unsafe, the verdict is unsafe even if
    others are unknown."""
    verdict = derive_broker_safety_verdict(
        configured_mode="live",
        readonly_flag=None,
        port=None,
        connected_account=None,
    )
    assert verdict.final_verdict == "unsafe"
    assert "configured_mode" in verdict.failing_gates


@pytest.mark.parametrize(
    "configured_mode,port,account,expected",
    [
        ("paper", 7497, "DU111", "paper-only"),
        ("paper", 7497, None, "unknown"),
        ("paper", 7496, "DU111", "unsafe"),
        ("live", 7497, "DU111", "unsafe"),
        ("paper", 7497, "U111", "unsafe"),
        ("paper", 8080, "DU111", "unknown"),
    ],
)
def test_derive_table(configured_mode, port, account, expected) -> None:
    verdict = derive_broker_safety_verdict(
        configured_mode=configured_mode,
        readonly_flag=True,
        port=port,
        connected_account=account,
    )
    assert verdict.final_verdict == expected


def test_broker_safety_verdict_model_serializes() -> None:
    """The Pydantic model serializes cleanly so the Frontend can consume
    it without bespoke decoders."""
    verdict = BrokerSafetyVerdict(
        configured_mode="paper",
        readonly_flag=True,
        port_class="paper_port",
        connected_account_prefix="DU",
        final_verdict="paper-only",
        failing_gates=[],
        unknown_gates=[],
    )
    payload = verdict.model_dump()
    assert payload["final_verdict"] == "paper-only"
    assert payload["failing_gates"] == []
