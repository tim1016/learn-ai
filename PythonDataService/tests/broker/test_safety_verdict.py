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


def test_derive_paper_only_when_readonly_false_post_adr0011_amendment() -> None:
    """PRD #619-A / ADR-0011 amendment — ``readonly_flag`` is no longer
    part of the identity derivation. An executing paper bot
    (``readonly=False``) on a paper port + DU account still reaches
    ``paper-only`` because submission capability is now an independent
    fact carried at the run/spec level, not on this verdict."""
    for ro in (None, False, True):
        verdict = derive_broker_safety_verdict(
            configured_mode="paper",
            readonly_flag=ro,
            port=7497,
            connected_account="DU1234567",
        )
        assert verdict.final_verdict == "paper-only", ro
        assert "readonly_flag" not in verdict.unknown_gates
        assert "readonly_flag" not in verdict.failing_gates


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


# ---------------------------------------------------------------------------
# PRD #619-A / ADR-0011 amendment — identity × capability × submit_mode
# Cartesian matrix. Identity is per-derivation only (this module); the
# Resume gate composition adds capability and uncertain-intent. The
# matrix asserts: identity is driven exclusively by (configured_mode,
# port, connected_account), and the same identity verdict is reached
# across every (readonly, submit_mode) cell.
# ---------------------------------------------------------------------------


_IDENTITY_CELLS = [
    # (mode, port, account, expected_identity)
    ("paper", 7497, "DU1234567", "paper-only"),
    ("paper", 4002, "DU0000001", "paper-only"),
    ("paper", 7496, "DU1234567", "unsafe"),  # paper mode but live port
    ("live", 7497, "DU1234567", "unsafe"),  # live mode
    ("paper", 7497, "U1234567", "unsafe"),  # non-DU account
    ("paper", 8080, "DU1234567", "unknown"),  # unknown port
    ("paper", 7497, None, "unknown"),  # no account
]


@pytest.mark.parametrize("mode,port,account,expected_identity", _IDENTITY_CELLS)
@pytest.mark.parametrize("readonly_flag", [True, False, None])
@pytest.mark.parametrize("submit_mode", ["live_paper", "shadow"])
def test_identity_derivation_independent_of_readonly_and_submit_mode(
    mode: str,
    port: int,
    account: str | None,
    expected_identity: str,
    readonly_flag: bool | None,
    submit_mode: str,
) -> None:
    """ADR-0011 amendment Cartesian: identity is a function of
    (configured_mode, port, account) only. ``readonly_flag`` and
    ``submit_mode`` do not move the verdict. submit_mode is consumed
    by the higher-altitude Resume gate composition, not this
    derivation."""
    # submit_mode is a sidecar parameter for matrix coverage — assert
    # that varying it never moves the derived identity.
    del submit_mode  # noqa: WPS420

    verdict = derive_broker_safety_verdict(
        configured_mode=mode,  # type: ignore[arg-type]
        readonly_flag=readonly_flag,
        port=port,
        connected_account=account,
    )

    assert verdict.final_verdict == expected_identity
