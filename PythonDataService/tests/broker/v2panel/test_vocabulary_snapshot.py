"""Contract tests for the closed broker-v2 panel vocabulary (S1, spec §13).

Pins the snapshot ↔ live-set parity, the copy-coverage rule (every emitted code
carries non-trivial server-authored copy), same-run Pause/Continue vocabulary,
and the reconciliation-verdict lockstep with the clerk model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import ReconciliationVerdict as ClerkVerdict
from app.broker.v2panel.vocabulary import (
    ALL_VOCABULARY_CODES,
    OPERATOR_COPY,
    RECONCILIATION_VERDICTS,
    copy_for,
)

_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "broker"
    / "v2panel"
    / "vocabulary.snapshot.json"
)


def test_snapshot_file_exists() -> None:
    assert _SNAPSHOT_PATH.exists(), (
        f"snapshot not found at {_SNAPSHOT_PATH} — regenerate via "
        "scripts/regenerate_broker_v2_vocabulary_snapshot.py"
    )


def test_snapshot_matches_live_vocabulary() -> None:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    snapshot_codes = set(snapshot["codes"])
    live_codes = set(ALL_VOCABULARY_CODES)

    missing = sorted(live_codes - snapshot_codes)
    extra = sorted(snapshot_codes - live_codes)
    assert not missing, f"codes in vocabulary.py missing from snapshot: {missing}"
    assert not extra, f"codes in snapshot not in vocabulary.py: {extra}"


def test_snapshot_codes_are_sorted_and_unique() -> None:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    codes = snapshot["codes"]
    assert codes == sorted(codes), "snapshot codes are not sorted"
    assert len(codes) == len(set(codes)), "snapshot codes contain duplicates"


def test_same_run_pause_and_continue_are_closed_vocabulary() -> None:
    """Pause is a live-run state and Continue is its identity-preserving verb."""
    assert "PAUSED" in ALL_VOCABULARY_CODES
    assert "pause" in ALL_VOCABULARY_CODES
    assert "continue" in ALL_VOCABULARY_CODES
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert {"PAUSED", "pause", "continue"}.issubset(snapshot["codes"])


def test_every_emitted_code_has_nontrivial_copy() -> None:
    """Decision #7: no code reaches the UI without server-authored copy."""
    for code in ALL_VOCABULARY_CODES:
        copy = copy_for(code)
        assert copy.label, f"missing label for {code}"
        assert copy.explanation, f"missing explanation for {code}"
        # The explanation must be more than a restatement of the code token.
        assert copy.explanation != code
        assert len(copy.explanation) > len(code)


def test_copy_map_has_no_orphan_entries() -> None:
    """Every copy entry corresponds to a real emitted code (no dead copy)."""
    orphans = sorted(set(OPERATOR_COPY) - set(ALL_VOCABULARY_CODES))
    assert not orphans, f"copy entries with no emitted code: {orphans}"


def test_reconciliation_verdicts_match_clerk_model() -> None:
    """The panel verdict set stays in lockstep with the clerk's StrEnum/Literal."""
    clerk_verdicts = set(ClerkVerdict.__args__)  # type: ignore[attr-defined]
    assert clerk_verdicts == RECONCILIATION_VERDICTS


def test_missing_copy_raises_keyerror() -> None:
    """copy_for surfaces a missing code immediately (never a silent passthrough)."""
    with pytest.raises(KeyError):
        copy_for("NOT_A_REAL_CODE")
