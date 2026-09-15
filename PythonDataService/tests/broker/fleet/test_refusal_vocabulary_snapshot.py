"""Committed-snapshot parity for the fleet's closed refusal vocabulary (#2067).

Decision 9: the reasons do NOT enter the exported OpenAPI contract. The
frontend's copy map locks against the committed snapshot instead, exactly as
the broker-v2 panel vocabulary does -- see
``scripts/regenerate_broker_v2_vocabulary_snapshot.py`` and
``.github/workflows/ci.yml``'s ``broker-v2-vocabulary-contract`` job for the
pattern this mirrors.

The live set is derived, not hand-listed: walking ``FleetControlError``'s
subclass closure means a new refusal family cannot be added without this
test noticing -- provided the module declaring it has been imported. Three
subclasses live outside ``errors.py`` (``presence.py``, ``confirmation.py``,
and the Alpaca-specific ``app/broker/alpaca/clerk/fleet_boot.py``); this
module force-imports all three below so the closure walk can see them
regardless of test collection order. ``refusal_vocabulary.py`` itself stays a
leaf and does not import any of the three (least of all the provider-specific
one) -- see its own docstring.

This file also carries Task 7b's regression: six refusal families had zero
``next_step`` across 31 raise sites (``clerk_unreachable`` -- a 503 the
operator can act on -- carried none at any of its 13). An AST walk over every
``.py`` file under ``app/`` enforces that going forward.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

# Force-import the three external FleetControlError-subclass-declaring
# modules so the live subclass closure below is complete regardless of
# which other test modules have already run in this process. The Alpaca
# import is fine *here* (a test file, not part of the generic fleet spine);
# `refusal_vocabulary.py` and `errors.py` themselves never import it --
# `test_import_isolation.py` fences that.
import app.broker.alpaca.clerk.fleet_boot
import app.broker.fleet.confirmation
import app.broker.fleet.presence  # noqa: F401
from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.refusal_vocabulary import (
    _MINTED_OUTSIDE_THE_CLOSURE,
    FLEET_REFUSAL_REASONS,
    _subclass_closure,
)

_APP_ROOT = Path(__file__).resolve().parents[3] / "app"
_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "broker"
    / "fleet"
    / "refusal_vocabulary.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[4]
    / "Frontend"
    / "src"
    / "app"
    / "fleet"
    / "fleet-refusal-vocabulary.snapshot.json"
)


# ---- 7a: the vocabulary itself ---------------------------------------------


def test_the_declared_set_equals_the_live_subclass_closure() -> None:
    """Every FleetControlError subclass in app/ is declared, and vice versa."""
    live = {cls.reason for cls in _subclass_closure(FleetControlError)}
    declared = set(FLEET_REFUSAL_REASONS) - _MINTED_OUTSIDE_THE_CLOSURE
    missing_from_declared = sorted(live - declared)
    missing_from_live = sorted(declared - live)
    assert not missing_from_declared, (
        f"live FleetControlError subclass(es) undeclared in FLEET_REFUSAL_REASONS: "
        f"{missing_from_declared}"
    )
    assert not missing_from_live, (
        f"FLEET_REFUSAL_REASONS declares code(s) no live subclass raises: {missing_from_live}"
    )


def test_every_minted_reason_literal_is_declared() -> None:
    """The five codes produced without a FleetControlError are declared too."""
    assert {
        "command_envelope_invalid",
        "compatibility_read_retired",
        "compatibility_retirement_state_invalid",
        "fleet_lane_capacity_exhausted",
        "qualification_market_status_unavailable",
    } == _MINTED_OUTSIDE_THE_CLOSURE
    assert set(FLEET_REFUSAL_REASONS) >= _MINTED_OUTSIDE_THE_CLOSURE


def test_the_closure_walk_would_notice_a_new_family() -> None:
    """The parity check is vacuous unless the walk actually finds subclasses.

    Without this, a broken ``_subclass_closure`` returning an empty set would
    make ``test_the_declared_set_equals_the_live_subclass_closure`` pass
    against an equally-empty ``declared`` filter -- the exact vacuous-over-an-
    empty-source failure mode this lane keeps re-discovering.
    """

    class _Probe(FleetControlError):
        reason = "probe_only_never_raised"

    try:
        assert "probe_only_never_raised" in {c.reason for c in _subclass_closure(FleetControlError)}
    finally:
        FleetControlError.__subclasses__()  # the probe is GC'd with the test


def test_committed_snapshot_matches_freshly_generated_output() -> None:
    from scripts.regenerate_fleet_refusal_vocabulary_snapshot import build_snapshot

    fresh = json.dumps(build_snapshot(), indent=2, sort_keys=False) + "\n"
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == fresh


def test_python_and_frontend_snapshots_are_byte_identical() -> None:
    if not _FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(f"Frontend/ not present in this checkout ({_FRONTEND_SNAPSHOT_PATH})")
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == _FRONTEND_SNAPSHOT_PATH.read_text(
        encoding="utf-8"
    )


def test_snapshot_reasons_are_sorted_and_cover_every_declared_code() -> None:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    reasons = snapshot["reasons"]
    assert list(reasons) == sorted(reasons)
    assert set(reasons) == set(FLEET_REFUSAL_REASONS)
    for code, entry in reasons.items():
        assert entry["status_code"] == FLEET_REFUSAL_REASONS[code].status_code
        assert entry["meaning"] == FLEET_REFUSAL_REASONS[code].meaning


# ---- 7b: the next_step backfill --------------------------------------------

#: Class-name -> reason-code map for the AST walk below, derived from the
#: live closure (not hand-duplicated) so a renamed or retired family cannot
#: silently drop out of the walk's vocabulary.
_FAMILY_REASON_BY_CLASS_NAME = {
    cls.__name__: cls.reason for cls in _subclass_closure(FleetControlError)
}

#: Six families had zero next_step across 31 raise sites at #2067's baseline;
#: clerk_unreachable is a 503 the operator can act on and carried none at any
#: of its 13.
_NEXT_STEP_REQUIRED = frozenset(
    {
        "broker_and_clerk_required",
        "clerk_account_mismatch",
        "clerk_broker_mismatch",
        "clerk_not_found",
        "clerk_unreachable",
        "fleet_control_error",
    }
)


def _raise_sites_without_next_step(root: Path) -> tuple[set[str], int]:
    """Reason codes with >=1 FleetControlError-family raise site lacking
    ``next_step``, plus the total number of family raise sites examined.

    The count is returned so callers can assert a floor and catch a walker
    that silently examines nothing (the vacuous-comprehension failure mode).
    """
    bare_reasons: set[str] = set()
    examined = 0
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            func = node.exc.func
            class_name = func.id if isinstance(func, ast.Name) else None
            reason = _FAMILY_REASON_BY_CLASS_NAME.get(class_name or "")
            if reason is None:
                continue
            examined += 1
            if not any(kw.arg == "next_step" for kw in node.exc.keywords):
                bare_reasons.add(reason)
    return bare_reasons, examined


def test_no_refusal_family_raises_without_a_next_step_everywhere() -> None:
    """Six families had zero next_step across 31 raise sites; clerk_unreachable
    is a 503 the operator can act on, and carried none at any of its 13."""
    bare, examined = _raise_sites_without_next_step(_APP_ROOT)
    # Anti-vacuous floor: a walker that silently examines nothing would make
    # the assertion below pass vacuously. #2067's baseline examined 187
    # FleetControlError-family raise sites across app/; a comfortable floor
    # below that tolerates legitimate future removals without masking a
    # broken walk.
    assert examined >= 150, (
        f"the raise-site walk only examined {examined} FleetControlError-family "
        "call sites across app/; expected at least 150 -- the walker may be broken"
    )
    offending = bare & _NEXT_STEP_REQUIRED
    assert offending == set(), (
        f"these families still have a raise site with no next_step: {sorted(offending)}"
    )
