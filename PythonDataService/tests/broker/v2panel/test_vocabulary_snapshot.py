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
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import HOLD_REASON_CODES
from app.broker.v2panel.vocabulary import (
    ACTION_IDS,
    CHANNEL_STATES,
    DESIRED_STATES,
    DUTY_OUTCOME_KINDS,
    HOLD_REASON_BY_STORED_CODE,
    HOLD_REASONS,
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
    hold_reason_for,
)

_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "broker"
    / "v2panel"
    / "vocabulary.snapshot.json"
)


def test_committed_snapshots_match_freshly_generated_output() -> None:
    """Regression for #1666: committed bytes must equal what the generator
    produces from live source right now, exercised in-process.
    """
    from scripts.regenerate_broker_v2_vocabulary_snapshot import build_snapshot

    fresh = json.dumps(build_snapshot(), indent=2, sort_keys=False) + "\n"
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == fresh


def test_reconciliation_verdicts_match_clerk_model() -> None:
    """The panel verdict set stays in lockstep with the clerk's StrEnum/Literal."""
    clerk_verdicts = set(ClerkVerdict.__args__)  # type: ignore[attr-defined]
    assert clerk_verdicts == RECONCILIATION_VERDICTS


# ── Literal ↔ collection parity ─────────────────────────────────────────────
# Every closed vocabulary in this module is declared twice: once as a
# ``Literal`` (for static type-checking on request/response schemas) and once
# as a runtime ``frozenset``/``tuple`` (for iteration — including by the
# ``ALL_VOCABULARY_CODES`` below). Nothing enforces the two stay equal.
#
# ``ActionId`` (a ``Literal``) and ``ACTION_IDS`` (a tuple) drifting apart is
# the failure this guards against: a member added to the ``Literal`` alone
# makes the request schema accept it while the copy-coverage test above stays
# green and every generated artifact (snapshot, manual) silently omits it. The
# same drift is possible for any of the other eight pairs, so all nine are
# swept here, not just ``ActionId``.
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

    Regression test: ``ActionId`` gained nine members
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


def test_every_registered_clerk_hold_cause_has_a_stored_code_row() -> None:
    """The clerk's registry decides which causes exist; this table must follow.

    ``vocabulary.py`` is a leaf and imports nothing from the clerk, so the
    lockstep lives here — the same arrangement
    ``test_reconciliation_verdicts_match_clerk_model`` uses. A cause
    registered in ``HOLD_REASON_CODES`` with no row here is not merely
    undocumented: every hold carrying it renders as ``UNKNOWN_HOLD`` on the
    operator's only view of an account-wide entry fence.
    """
    unnameable = sorted(HOLD_REASON_CODES - set(HOLD_REASON_BY_STORED_CODE))

    assert not unnameable, (
        "clerk hold cause(s) the panel cannot name — add a row to "
        f"HOLD_REASON_BY_STORED_CODE (plus its HoldReason member and copy): {unnameable}"
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
