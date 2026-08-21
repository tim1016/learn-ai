"""Strategy-catalog module tests (#1703, PRD #1697 S6 — "catalog honesty").

Covers the two acceptance-criteria behaviors that motivate the new module:

- A validated strategy with no registered runtime is composed as a visible,
  non-selectable row with a backend-authored reason distinguishable from a
  stale-proof block — never silently dropped.
- The retired enum-to-runtime invariant test
  (``test_every_admitted_alpaca_paper_strategy_has_a_runtime`` in
  ``test_bot_runner.py``) is replaced here by a single property test over
  every real registry entry: whatever is selectable is launchable, and no
  launchable, validated strategy is hidden from the catalog.

No ``hypothesis`` dependency exists in this repo yet (checked
``requirements-dev.txt``); adding one for a single test isn't justified, so
the property test is an exhaustive sweep over every real committed
validation entry rather than a generated-input property test.
"""

from __future__ import annotations

import pytest

from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys
from app.services.broker_v2_panel import strategy_catalog
from app.services.broker_v2_panel.paper_deploy_service import _strategy_views
from app.services.strategy_validation_manifest import (
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from tests.broker.v2panel.conftest import _accepted_deploy_entry


def test_validated_strategy_without_runtime_is_visible_but_not_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1703 AC1: a validated, no-runtime strategy gets a blocked row, never a silent absence."""
    entry = _accepted_deploy_entry()
    monkeypatch.setattr(strategy_catalog, "supported_alpaca_paper_strategy_keys", lambda: frozenset())

    rows = _strategy_views([entry])

    assert [row.strategy_key for row in rows] == [entry.strategy_key]
    row = rows[0]
    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.admissible_modes == ()
    assert row.blocked_explanation is not None
    assert "runtime" in row.blocked_explanation.lower()


def test_no_runtime_block_reads_differently_from_a_stale_proof_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1703: "not built yet" must be distinguishable from "not validated" / a stale proof."""
    stale_proof_entry = _accepted_deploy_entry().model_copy(update={"audit_copy_sha256": "0" * 64})
    no_runtime_entry = _accepted_deploy_entry()
    monkeypatch.setattr(strategy_catalog, "supported_alpaca_paper_strategy_keys", lambda: frozenset())

    no_runtime_rows = _strategy_views([no_runtime_entry])
    monkeypatch.undo()
    stale_proof_rows = _strategy_views([stale_proof_entry])

    no_runtime_reason = no_runtime_rows[0].blocked_explanation
    stale_proof_reason = stale_proof_rows[0].blocked_explanation
    assert no_runtime_reason is not None
    assert stale_proof_reason is not None
    assert no_runtime_reason != stale_proof_reason
    # A no-runtime row cannot even Dry Run; a stale-proof row still can.
    assert no_runtime_rows[0].admissible_modes == ()
    assert stale_proof_rows[0].admissible_modes == ("dry_run",)


def test_selectable_rows_are_exactly_the_launchable_and_visible_rows() -> None:
    """#1703 replacement for the retired enum-to-runtime invariant test.

    Exhaustive sweep over every real committed, currently-validated
    registry entry, asserting the two halves of the acceptance criterion
    "whatever is selectable is launchable, and nothing the runner can
    execute is hidden":

    1. Every selectable row's strategy_key has a registered runtime.
    2. Every validated entry whose strategy_key has a registered runtime
       produces a row in the catalog — it is never silently dropped.
    """
    seeds = strategy_registry_seeds()
    entries = load_strategy_validation_entries(seeds)
    validated_entries = [
        entry
        for entry in entries
        if entry.validation_state == "validated"
        and entry.current_flag_event is not None
        and entry.current_flag_event.flag == "validated"
    ]
    assert validated_entries, "fixture sanity: at least one real strategy must be validated"
    runtime_keys = supported_alpaca_paper_strategy_keys()

    rows = _strategy_views(validated_entries)
    rows_by_key = {row.strategy_key: row for row in rows}

    for row in rows:
        if row.selectable:
            assert row.strategy_key in runtime_keys, (
                f"{row.strategy_key} is selectable but the runner has no registered runtime for it."
            )
    for entry in validated_entries:
        if entry.strategy_key in runtime_keys:
            assert entry.strategy_key in rows_by_key, (
                f"{entry.strategy_key} has a registered runtime and is validated, "
                "but is missing from the catalog."
            )
