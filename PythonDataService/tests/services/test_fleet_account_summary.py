"""PRD #616 — pure-function tests for ``compute_fleet_account_summary``
and ``compute_account_identity``.

Coverage of the ``account_identity`` enum branches, the
``account_identity_reason_codes`` vocabulary, and the no-overload
rule: configuration disagreement never raises ``verdict='contaminated'``.
"""

from __future__ import annotations

import pytest

from app.engine.live.fleet import (
    compute_account_identity,
    compute_fleet_account_summary,
)

# ---------------------------------------------------------------------------
# compute_account_identity — the identity-only pure function
# ---------------------------------------------------------------------------


def test_no_instances_is_unknown() -> None:
    out = compute_account_identity({}, broker_connected_account=None, broker_account_known=False)
    assert out["account_identity"] == "UNKNOWN"
    assert out["account_id"] is None
    assert out["account_identity_reason_codes"] == []


def test_all_instances_missing_account_id_is_unknown() -> None:
    out = compute_account_identity(
        {"sid-a": None, "sid-b": None},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert out["account_identity"] == "UNKNOWN"
    assert "ACCOUNT_ID_MISSING" in out["account_identity_reason_codes"]


def test_single_instance_consistent() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
    )
    assert out["account_identity"] == "CONSISTENT"
    assert out["account_id"] == "DU284968"
    assert out["account_identity_reason_codes"] == []


def test_two_instances_agree_consistent_with_broker_match() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968", "sid-b": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
    )
    assert out["account_identity"] == "CONSISTENT"
    assert out["account_id"] == "DU284968"
    assert out["account_identity_reason_codes"] == []


def test_two_instances_disagree_is_conflicting() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968", "sid-b": "DU111111"},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert out["account_identity"] == "CONFLICTING"
    assert "INSTANCE_ACCOUNT_MISMATCH" in out["account_identity_reason_codes"]


def test_broker_account_mismatch_is_conflicting() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968"},
        broker_connected_account="DU777777",
        broker_account_known=True,
    )
    assert out["account_identity"] == "CONFLICTING"
    assert "BROKER_ACCOUNT_MISMATCH" in out["account_identity_reason_codes"]


def test_broker_unavailable_surfaces_reason_but_consistent() -> None:
    # Without a broker signal we cannot disagree → identity stays
    # CONSISTENT but the reason is surfaced for the cockpit's row.
    out = compute_account_identity(
        {"sid-a": "DU284968"},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert out["account_identity"] == "CONSISTENT"
    assert "BROKER_ACCOUNT_UNAVAILABLE" in out["account_identity_reason_codes"]


def test_mixed_known_and_unknown_account_ids() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968", "sid-b": None, "sid-c": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
    )
    # Mixed missing+known is reported but identity is CONFLICTING due
    # to ACCOUNT_ID_MISSING (we can't prove consistency without all
    # ids).
    assert "ACCOUNT_ID_MISSING" in out["account_identity_reason_codes"]
    assert out["account_id"] == "DU284968"
    assert out["account_identity"] == "CONFLICTING"


def test_three_ids_two_match_canonical_is_majority() -> None:
    out = compute_account_identity(
        {"sid-a": "DU284968", "sid-b": "DU284968", "sid-c": "DU111111"},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert out["account_identity"] == "CONFLICTING"
    assert out["account_id"] == "DU284968"
    assert "INSTANCE_ACCOUNT_MISMATCH" in out["account_identity_reason_codes"]


# ---------------------------------------------------------------------------
# compute_fleet_account_summary — composition with contamination
# ---------------------------------------------------------------------------


def test_summary_clean_consistent() -> None:
    payload = compute_fleet_account_summary(
        net_positions={"SPY": 0},
        explained_by_instance={"sid-a": {"SPY": 0}},
        instance_account_ids={"sid-a": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
    )
    assert payload["account_identity"] == "CONSISTENT"
    assert payload["contamination"]["verdict"] == "clean"
    assert payload["account_identity_reason_codes"] == []


def test_summary_account_conflict_does_not_imply_contamination() -> None:
    # PRD invariant: identity disagreement never raises verdict.
    payload = compute_fleet_account_summary(
        net_positions={},
        explained_by_instance={"sid-a": {}, "sid-b": {}},
        instance_account_ids={"sid-a": "DU284968", "sid-b": "DU111111"},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert payload["account_identity"] == "CONFLICTING"
    assert payload["contamination"]["verdict"] == "clean"


def test_summary_contamination_does_not_imply_account_conflict() -> None:
    payload = compute_fleet_account_summary(
        net_positions={"SPY": 1},
        explained_by_instance={"sid-a": {}},
        instance_account_ids={"sid-a": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
    )
    assert payload["account_identity"] == "CONSISTENT"
    assert payload["contamination"]["verdict"] == "contaminated"


def test_summary_passes_policy_blocks_starts_only_when_contaminated() -> None:
    payload = compute_fleet_account_summary(
        net_positions={"SPY": 0},
        explained_by_instance={"sid-a": {"SPY": 0}},
        instance_account_ids={"sid-a": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
        policy_blocks_starts=True,
    )
    assert payload["contamination"]["policy_blocks_starts"] is False

    payload_dirty = compute_fleet_account_summary(
        net_positions={"SPY": 1},
        explained_by_instance={"sid-a": {}},
        instance_account_ids={"sid-a": "DU284968"},
        broker_connected_account="DU284968",
        broker_account_known=True,
        policy_blocks_starts=True,
    )
    assert payload_dirty["contamination"]["policy_blocks_starts"] is True


def test_summary_does_not_drop_reason_codes_with_consistent_identity() -> None:
    # Even when identity ends up CONSISTENT, an unknown broker
    # account surfaces the diagnostic reason for the row.
    payload = compute_fleet_account_summary(
        net_positions={"SPY": 0},
        explained_by_instance={"sid-a": {"SPY": 0}},
        instance_account_ids={"sid-a": "DU284968"},
        broker_connected_account=None,
        broker_account_known=False,
    )
    assert payload["account_identity"] == "CONSISTENT"
    assert "BROKER_ACCOUNT_UNAVAILABLE" in payload["account_identity_reason_codes"]


@pytest.mark.parametrize(
    "code",
    [
        "ACCOUNT_ID_MISSING",
        "INSTANCE_ACCOUNT_MISMATCH",
        "BROKER_ACCOUNT_UNAVAILABLE",
        "BROKER_ACCOUNT_MISMATCH",
    ],
)
def test_reason_code_vocabulary_pinned(code: str) -> None:
    """The Frontend lookup is exhaustive; pin the closed vocabulary."""
    # Exercise a case for each code so each is exercised end to end.
    if code == "ACCOUNT_ID_MISSING":
        out = compute_account_identity(
            {"sid-a": None},
            broker_connected_account=None,
            broker_account_known=False,
        )
    elif code == "INSTANCE_ACCOUNT_MISMATCH":
        out = compute_account_identity(
            {"sid-a": "DU1", "sid-b": "DU2"},
            broker_connected_account=None,
            broker_account_known=False,
        )
    elif code == "BROKER_ACCOUNT_UNAVAILABLE":
        out = compute_account_identity(
            {"sid-a": "DU1"},
            broker_connected_account=None,
            broker_account_known=False,
        )
    else:  # BROKER_ACCOUNT_MISMATCH
        out = compute_account_identity(
            {"sid-a": "DU1"},
            broker_connected_account="DU2",
            broker_account_known=True,
        )
    assert code in out["account_identity_reason_codes"]
