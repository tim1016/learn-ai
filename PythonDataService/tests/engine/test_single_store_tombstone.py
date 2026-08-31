"""Tombstone for the retired policy store (#1893, ADR 0049).

The lake is the only place historical bar data lives. Everything this module
asserts is an *absence*, which is exactly the kind of property that rots
silently: a reintroduced write path or a second root resolver breaks no
existing test, it just quietly gives the platform two stores again and makes
"which bytes did this run consume" unanswerable.

These are source-level assertions on purpose. A behavioural test cannot
observe a store that is not there; only reading the tree can.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.engine.data import policy_store

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _python_sources() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


# --------------------------------------------------------------------------
# The feature flag
# --------------------------------------------------------------------------

def test_the_data_lake_flag_is_not_a_setting() -> None:
    """DATA_LAKE_ENABLED cannot be configured, so it cannot be read.

    Asserted on the field rather than by grepping for the name: the string
    still appears in comments that explain what #1893 removed, and those are
    documentation, not a live branch.
    """
    assert "DATA_LAKE_ENABLED" not in Settings.model_fields
    assert not hasattr(settings, "DATA_LAKE_ENABLED")


def test_no_module_reads_the_data_lake_flag() -> None:
    """No executable read of the flag survives anywhere in ``app/``."""
    live_read = re.compile(
        r"settings\.DATA_LAKE_ENABLED"
        r"|getenv\(\s*[\"']DATA_LAKE_ENABLED"
        r"|environ\[\s*[\"']DATA_LAKE_ENABLED"
        r"|environ\.get\(\s*[\"']DATA_LAKE_ENABLED"
    )
    offenders = [
        f"{source.relative_to(APP_ROOT)}:{number}"
        for source in _python_sources()
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1)
        if live_read.search(line)
    ]
    assert offenders == [], f"the retired data-lake flag is read again at: {offenders}"


# --------------------------------------------------------------------------
# The policy store's write path
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "symbol",
    [
        "resolve_cache_root",
        "resolve_reference_root",
        "resolve_policy_root",
        "symbol_write_lock",
        "record_fetch",
        "PROVENANCE_SCHEMA_VERSION",
    ],
)
def test_policy_store_write_path_symbol_is_gone(symbol: str) -> None:
    """Each name below belonged to the store's write side or its private roots."""
    assert not hasattr(policy_store, symbol), (
        f"policy_store.{symbol} is back; the policy store had exactly one write "
        "boundary and #1893 removed it in favour of the lake's ensure_data"
    )


def test_the_full_range_exporter_is_gone() -> None:
    from app.engine.data import polygon_export

    assert not hasattr(polygon_export, "export_polygon_range_to_lean")


def test_the_lean_export_route_is_not_registered() -> None:
    """/api/engine/export-lean was the store's only HTTP write surface."""
    from app.main import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/engine/export-lean" not in paths


def test_the_range_ensurer_is_gone() -> None:
    """``availability.ensure_range`` fetched a whole range into the store."""
    from app.engine.data import availability

    assert not hasattr(availability, "ensure_range")


# --------------------------------------------------------------------------
# Root resolution
# --------------------------------------------------------------------------

def test_the_engine_read_seam_resolves_the_lake_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.data_lake import path_policy

    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(tmp_path))

    for adjusted in (True, False):
        roots = policy_store.resolve_data_roots(source="polygon", adjusted=adjusted)
        mode = "polygon_split_adjusted" if adjusted else "raw"
        assert roots == [path_policy.lake_root_within(tmp_path, mode)], (
            "the engine/chart read seam must answer with exactly one root, the "
            f"lake's {mode} root -- a second root here is a second store"
        )


# Modules that still resolve a LEAN data root from LEAN_DATA_ROOT /
# LEAN_DATA_CACHE rather than from the lake. These predate #1893 and sit
# outside the read paths ADR 0049 Decision 1c brought onto the lake (the two
# engines and the chart/indicator routers); retiring the policy store did not
# change them, and this tombstone deliberately does not claim it did.
#
# The allowlist is the point: it is what makes this test fail when a *new*
# root resolver appears. Do not add to it to make a failure go away -- a new
# entry means the platform grew a second way to find bar data, which is the
# thing the single-store invariant exists to prevent.
_ROOT_RESOLVERS_OUTSIDE_THE_LAKE = {
    "research/ml/generate_prediction_set.py",
    "research/runs/ledger.py",
    "routers/spec_strategy.py",
}


def test_no_new_module_resolves_lean_roots_from_the_environment() -> None:
    reads_env_root = re.compile(r"[\"']LEAN_DATA_CACHE[\"']")
    found = {
        str(source.relative_to(APP_ROOT))
        for source in _python_sources()
        if reads_env_root.search(source.read_text(encoding="utf-8"))
    }

    new = found - _ROOT_RESOLVERS_OUTSIDE_THE_LAKE
    assert new == set(), (
        f"new module(s) resolving a LEAN data root outside the lake: {sorted(new)}. "
        "The lake is the single store for historical bars (ADR 0049); route this "
        "through app.data_lake rather than widening the allowlist."
    )

    departed = _ROOT_RESOLVERS_OUTSIDE_THE_LAKE - found
    assert departed == set(), (
        f"allowlisted module(s) no longer resolve a LEAN root from the environment: "
        f"{sorted(departed)}. That is progress -- remove them from "
        "_ROOT_RESOLVERS_OUTSIDE_THE_LAKE so the list keeps shrinking."
    )
