"""Contract tests for the closed broker-v2 panel vocabulary (S1, spec §13).

Pins the snapshot ↔ live-set parity, the copy-coverage rule (every emitted code
carries non-trivial server-authored copy), same-run Pause/Continue vocabulary,
and the reconciliation-verdict lockstep with the clerk model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from app.broker.alpaca.clerk.models import ReconciliationVerdict as ClerkVerdict
from app.broker.v2panel.vocabulary import (
    ACTION_IDS,
    ALL_VOCABULARY_CODES,
    CHANNEL_STATES,
    DESIRED_STATES,
    DUTY_OUTCOME_KINDS,
    HOLD_REASON_BY_STORED_CODE,
    HOLD_REASONS,
    OPERATOR_COPY,
    PHASES,
    RECONCILIATION_VERDICTS,
    STATION_IDS,
    STATION_STATES,
    ActionId,
    ChannelState,
    DesiredState,
    DutyOutcomeKind,
    HoldReason,
    Phase,
    ReconciliationVerdict,
    StationId,
    StationState,
    copy_for,
    hold_reason_for,
)

_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "broker"
    / "v2panel"
    / "vocabulary.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[4]
    / "Frontend"
    / "src"
    / "app"
    / "components"
    / "broker"
    / "v2-panel"
    / "lib"
    / "broker-v2-vocabulary.snapshot.json"
)


def test_snapshot_file_exists() -> None:
    assert _SNAPSHOT_PATH.exists(), (
        f"snapshot not found at {_SNAPSHOT_PATH} — regenerate via "
        "scripts/regenerate_broker_v2_vocabulary_snapshot.py"
    )


def test_python_and_frontend_snapshots_are_byte_identical() -> None:
    """The two committed copies must never diverge from each other.

    Regression for #1666: at commit a16571c2, both files were hand-edited to
    carry the *same* wrong copy — this test alone would not have caught that
    (see test_snapshot_copy_matches_live_operator_copy_exactly for the test
    that does), but it does catch the more common case of only one file being
    hand-edited.

    Skipped when ``Frontend/`` isn't part of this checkout (e.g. the
    Python-only qualification container, whose image context is
    ``PythonDataService`` — see ``compose.yaml``): cross-tree parity is
    proven byte-for-byte there instead by the ``broker-v2-vocabulary-contract``
    CI job, which runs against a full checkout.
    """
    if not _FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(f"Frontend/ not present in this checkout ({_FRONTEND_SNAPSHOT_PATH})")
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == _FRONTEND_SNAPSHOT_PATH.read_text(encoding="utf-8")


def test_committed_snapshots_match_freshly_generated_output() -> None:
    """Regression for #1666: committed bytes must equal what the generator
    produces from live source right now — the same check CI's
    ``broker-v2-vocabulary-contract`` job performs via `git diff --exit-code`
    after regeneration, exercised here in-process.

    Only the Python-local snapshot is checked here — see
    ``test_python_and_frontend_snapshots_are_byte_identical`` for why the
    Frontend copy isn't compared directly from this module, and for the
    ``Python == Frontend`` half of the byte-identity that, combined with
    this test's ``Python == fresh``, transitively proves ``Frontend ==
    fresh`` whenever both tests run together in a full checkout.
    """
    from scripts.regenerate_broker_v2_vocabulary_snapshot import build_snapshot

    fresh = json.dumps(build_snapshot(), indent=2, sort_keys=False) + "\n"
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == fresh


def test_snapshot_copy_matches_live_operator_copy_exactly() -> None:
    """Regression for #1666: the committed `copy` must equal live label/explanation
    exactly, not merely be non-trivial. This is the test that would have caught
    the a16571c2 incident (both snapshots carrying the same wrong `resume.label`)."""
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    for code in ALL_VOCABULARY_CODES:
        live = copy_for(code)
        assert snapshot["copy"][code] == {"label": live.label, "explanation": live.explanation}, (
            f"{code}: committed snapshot copy diverges from live OPERATOR_COPY"
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


# ── Literal ↔ collection parity (ADR 0041 decision 6) ────────────────────────
# Every closed vocabulary in this module is declared twice: once as a
# ``Literal`` (for static type-checking on request/response schemas) and once
# as a runtime ``frozenset``/``tuple`` (for iteration — including by the
# operator-manual generator, ``scripts/regenerate_broker_v2_operator_manual.py``,
# and by ``ALL_VOCABULARY_CODES`` below). Nothing enforces the two stay equal.
#
# ``ActionId`` (a ``Literal``) and ``ACTION_IDS`` (a tuple) drifting apart is
# the exact failure this ADR names: a member added to the ``Literal`` alone
# makes the request schema accept it while the copy-coverage test above stays
# green and every generated artifact (snapshot, manual) silently omits it. The
# same drift is possible for any of the other eight pairs, so all nine are
# swept here, not just the one the ADR calls out by name.
_LITERAL_COLLECTION_PAIRS = (
    ("Phase", Phase, PHASES),
    ("DesiredState", DesiredState, DESIRED_STATES),
    ("DutyOutcomeKind", DutyOutcomeKind, DUTY_OUTCOME_KINDS),
    ("HoldReason", HoldReason, HOLD_REASONS),
    ("ReconciliationVerdict", ReconciliationVerdict, RECONCILIATION_VERDICTS),
    ("ChannelState", ChannelState, CHANNEL_STATES),
    ("StationId", StationId, STATION_IDS),
    ("StationState", StationState, STATION_STATES),
    ("ActionId", ActionId, ACTION_IDS),
)


@pytest.mark.parametrize(
    "name, literal, collection", _LITERAL_COLLECTION_PAIRS, ids=[p[0] for p in _LITERAL_COLLECTION_PAIRS]
)
def test_literal_matches_runtime_collection(name: str, literal: object, collection: object) -> None:
    """A ``Literal`` alias and its runtime collection must name the same codes.

    Regression test for ADR 0041 decision 6: ``ActionId`` gained nine members
    over time that ``ACTION_IDS`` never received, and nothing failed. Asserting
    ``set(get_args(...)) == set(collection)`` for every pair closes the gap so a
    future addition to one side without the other fails here, not by shipping an
    undocumented action.
    """
    literal_members = set(get_args(literal))
    collection_members = set(collection)
    missing_from_collection = sorted(literal_members - collection_members)
    missing_from_literal = sorted(collection_members - literal_members)
    assert not missing_from_collection, (
        f"{name}: in the Literal but not the runtime collection: {missing_from_collection}"
    )
    assert not missing_from_literal, (
        f"{name}: in the runtime collection but not the Literal: {missing_from_literal}"
    )


# ── Hold-reason reachability ─────────────────────────────────────────────────
# ``hold_reason_for`` narrows a stored clerk code into ``HoldReason`` through
# ``HOLD_REASON_BY_STORED_CODE``. A cause added to the Literal but not to that
# table is not merely undocumented — it is unreachable, and every hold carrying
# it renders as ``UNKNOWN_HOLD`` while looking supported everywhere else.

_HOLD_SENTINELS = frozenset({"NO_HOLD", "UNKNOWN_HOLD"})


def test_every_nameable_hold_reason_is_reachable_from_a_stored_code() -> None:
    """No `HoldReason` cause may exist that no stored code can produce."""
    causes = HOLD_REASONS - _HOLD_SENTINELS
    reachable = set(HOLD_REASON_BY_STORED_CODE.values())

    assert causes == reachable, (
        "unreachable hold cause(s) — add the stored code(s) the clerk writes "
        f"to HOLD_REASON_BY_STORED_CODE: {sorted(causes - reachable)}; "
        f"mapped to a code outside HoldReason: {sorted(reachable - causes)}"
    )


def test_the_sentinels_are_never_reachable_as_a_stored_cause() -> None:
    """`NO_HOLD` and `UNKNOWN_HOLD` describe the seam, never a journalled cause.

    A stored code mapping onto either would let the clerk assert "no hold"
    while a hold is active — the exact failure the narrowing exists to stop.
    """
    assert not _HOLD_SENTINELS & set(HOLD_REASON_BY_STORED_CODE.values())


def test_an_active_hold_never_narrows_to_no_hold() -> None:
    """Fail-closed, swept over every code the table knows plus an unknown one."""
    stored_codes = [*HOLD_REASON_BY_STORED_CODE, "SOME_FUTURE_HOLD_CAUSE", None, ""]

    assert all(
        hold_reason_for(active=True, stored_code=code) != "NO_HOLD"
        for code in stored_codes
    )
    assert all(
        hold_reason_for(active=False, stored_code=code) == "NO_HOLD"
        for code in stored_codes
    )
