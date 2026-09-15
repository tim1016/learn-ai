"""Committed-snapshot parity for the fleet's operation catalog (#2076, #2103).

Mirrors the established pattern for the broker-v2 panel vocabulary and the
fleet refusal vocabulary -- see ``scripts/regenerate_broker_v2_vocabulary_snapshot.py``
and ``tests/broker/fleet/test_refusal_vocabulary_snapshot.py`` for the
precedent this follows. The live catalog is derived from
``production_provider_adapters()``, not hand-listed, so a new, renamed or
rescoped operation cannot land without this test noticing.

``test_operation_catalog.py`` (this same directory) tests
``validate_operation_catalog``'s structural rules and a handful of pinned
Alpaca routes; this file is the committed-snapshot contract instead --
narrower in scope, but exhaustive over the full catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.fleet_composition import production_provider_adapters
from scripts.regenerate_fleet_operation_catalog_snapshot import build_snapshot

_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "app"
    / "broker"
    / "fleet"
    / "operation_catalog.snapshot.json"
)
_FRONTEND_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[4]
    / "Frontend"
    / "src"
    / "app"
    / "fleet"
    / "fleet-operation-catalog.snapshot.json"
)


def _live_operations() -> dict[str, dict[str, object]]:
    """The live Alpaca catalog, shaped exactly like a snapshot's ``operations`` map."""
    alpaca = production_provider_adapters()["alpaca"]
    return {
        operation.operation_id: {
            "method": operation.method,
            "path_template": operation.path_template,
            "requires_account": operation.requires_effective_account,
        }
        for operation in alpaca.operations()
    }


def test_the_live_catalog_is_not_vacuous() -> None:
    """Anti-vacuous floor: a broken registry returning an empty catalog would
    make every parity assertion below pass vacuously against an equally
    empty snapshot. #2103's baseline measured 76 operations; a comfortable
    floor below that tolerates legitimate future removals without masking a
    catalog that silently stopped being populated."""
    live = _live_operations()
    assert len(live) >= 50, (
        f"production_provider_adapters()['alpaca'].operations() only yielded "
        f"{len(live)} operations; expected at least 50 -- the registry may be broken"
    )


def test_the_declared_set_equals_the_live_catalog() -> None:
    """Every live operation is in the snapshot, with matching method, path
    template and account-scoping -- and vice versa."""
    live = _live_operations()
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    declared = snapshot["operations"]

    missing_from_snapshot = sorted(set(live) - set(declared))
    missing_from_live = sorted(set(declared) - set(live))
    assert not missing_from_snapshot, (
        f"live operation(s) undeclared in the committed snapshot: {missing_from_snapshot}"
    )
    assert not missing_from_live, (
        f"snapshot declares operation(s) no live adapter serves: {missing_from_live}"
    )
    mismatched = sorted(
        operation_id
        for operation_id in live
        if live[operation_id] != declared[operation_id]
    )
    assert not mismatched, (
        f"operation(s) whose live method/path_template/requires_account disagree "
        f"with the committed snapshot: {mismatched}"
    )


def test_committed_snapshot_matches_freshly_generated_output() -> None:
    fresh = json.dumps(build_snapshot(), indent=2, sort_keys=False) + "\n"
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == fresh


def test_python_and_frontend_snapshots_are_byte_identical() -> None:
    if not _FRONTEND_SNAPSHOT_PATH.exists():
        pytest.skip(f"Frontend/ not present in this checkout ({_FRONTEND_SNAPSHOT_PATH})")
    assert _SNAPSHOT_PATH.read_text(encoding="utf-8") == _FRONTEND_SNAPSHOT_PATH.read_text(
        encoding="utf-8"
    )


def test_snapshot_operations_are_sorted_by_operation_id() -> None:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    operations = snapshot["operations"]
    assert list(operations) == sorted(operations)


def test_adapter_version_matches_the_live_adapter() -> None:
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    alpaca = production_provider_adapters()["alpaca"]
    assert snapshot["adapter_version"] == alpaca.adapter_version


def test_bots_deploy_apply_stays_gone() -> None:
    """Regression guard for D-D (#2118): a deleted operation reappearing
    undeclared-but-still-routable would not be caught by set equality above
    if it were re-added under the same name with different shape by
    accident; naming it here makes the historical fact explicit and
    independently checkable."""
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert "bots_deploy_apply" not in snapshot["operations"]


def test_manual_order_cancel_declares_a_path_converter_on_order_ref() -> None:
    """The one operation `operationUrl`'s `:path` handling exists for --
    pinned here so a rename or converter removal on the Python side is
    caught independently of the Frontend's own contract test."""
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    template = snapshot["operations"]["manual_order_cancel"]["path_template"]
    assert "{order_ref:path}" in template
