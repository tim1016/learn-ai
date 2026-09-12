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

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_validation import service as golden_validation_service
from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys
from app.services.broker_v2_panel import panel_deploy, strategy_catalog
from app.services.broker_v2_panel.paper_deploy_service import _strategy_views
from app.services.broker_v2_panel.strategy_catalog import GoldenValidationScope
from app.services.canary_admission import apply_canary_activation, plan_canary_activation
from app.services.strategy_validation_manifest import (
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from tests.broker.v2panel.conftest import _accepted_deploy_entry
from tests.broker.v2panel.fixtures import ACCT


def _golden_scope(symbol: str = "TSLA", **parameters: object) -> GoldenValidationScope:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    resolved = registration.param_schema(symbol=symbol).model_dump(mode="json")
    resolved.update(parameters)
    return GoldenValidationScope(symbol=symbol, parameters=resolved)


def test_validated_strategy_without_runtime_is_visible_but_not_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1703 AC1: a validated, no-runtime strategy gets a blocked row, never a silent absence."""
    entry = _accepted_deploy_entry()
    monkeypatch.setattr(strategy_catalog, "supported_alpaca_paper_strategy_keys", lambda: frozenset())

    rows = _strategy_views([entry], account_id=ACCT, custody_world="real_paper")

    assert [row.strategy_key for row in rows] == [entry.strategy_key]
    row = rows[0]
    assert row.evidence_status == "blocked"
    assert row.paper_access_state == "blocked"
    assert row.selectable is False
    assert row.admissible_modes == ()
    assert row.blocked_explanation is not None
    assert "runtime" in row.blocked_explanation.lower()


def test_catalog_reads_a_confirmed_durable_pairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    monkeypatch.setattr(
        "app.services.canary_admission.DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
        ledger_path,
    )
    entry = _accepted_deploy_entry()
    blocked = _strategy_views([entry], account_id=ACCT, custody_world="real_paper")[0]
    assert blocked.paper_access_state == "available"
    assert blocked.selectable is False

    plan = plan_canary_activation(
        program_key="ema_crossover_signal",
        account_id=ACCT,
        actor="local:test-operator",
        reason="Reviewed EMA paper canary.",
    )
    apply_canary_activation(plan=plan, confirmation_token=plan.confirmation_token)

    admitted = _strategy_views([entry], account_id=ACCT, custody_world="real_paper")[0]
    assert admitted.paper_access_state == "enabled"
    assert admitted.selectable is True
    assert "paper" in admitted.admissible_modes


def test_current_golden_validation_can_supply_the_validation_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed Golden case reaches Paper without requiring a duplicate v1 event."""
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )

    rows = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={"ema_crossover_signal": (_golden_scope(),)},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.strategy_key == "ema_crossover_signal"
    assert row.validation_case_symbol == "TSLA"
    assert row.validation_case_parameters == {
        "gap": 0.2,
        "gap_bps": 0.0,
        "rsi_min": 50.0,
        "rsi_max": 70.0,
    }
    assert row.golden_validation_scope is True
    assert row.evidence_status == "accepted"
    assert row.selectable is True
    assert row.admissible_modes == ("dry_run", "paper")


@pytest.mark.asyncio
async def test_panel_golden_catalog_uses_uncapped_accepted_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_contract is not None
    old_but_current = SimpleNamespace(
        latest_review=SimpleNamespace(
            decision="accept",
            authorized_program_version=None,
        ),
        review_is_current=True,
        validation_case={
            "strategy": {
                "name": "ema_crossover_signal",
                "program_version": registration.signal_program_contract.program_version,
            },
            "symbol": "SPY",
            "parameters": _golden_scope("SPY").parameters,
        },
    )

    async def accepted_lookup(operation, **kwargs):
        assert operation is golden_validation_service.list_latest_accepted_dossiers
        assert kwargs == {"symbol": None}
        return [old_but_current]

    monkeypatch.setattr(panel_deploy, "with_connection", accepted_lookup)

    assert await panel_deploy._current_golden_validation_scopes(None) == {
        "ema_crossover_signal": (_golden_scope("SPY"),)
    }


def test_golden_validation_never_bypasses_program_account_access() -> None:
    rows = _strategy_views(
        [],
        account_id="not-allowlisted",
        custody_world="real_paper",
        golden_validation_scopes={"ema_crossover_signal": (_golden_scope(),)},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_status == "accepted"
    assert row.paper_access_state == "available"
    assert row.selectable is False
    assert row.admissible_modes == ("dry_run",)


def test_nondefault_golden_scope_is_selectable_with_its_reviewed_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Golden row carries the exact reviewed controls instead of falling back to defaults."""
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )

    rows = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={"ema_crossover_signal": (_golden_scope(gap=0.75),)},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_status == "accepted"
    assert row.selectable is True
    assert row.admissible_modes == ("dry_run", "paper")
    assert row.validation_case_symbol == "TSLA"
    assert row.validation_case_parameters["gap"] == 0.75
    assert row.golden_validation_scope is True


def test_incomplete_golden_scope_fails_closed_instead_of_using_current_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historic case missing fields cannot be silently upgraded by today's registry defaults."""
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )

    rows = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={
            "ema_crossover_signal": (GoldenValidationScope(symbol="TSLA", parameters={"gap": 0.75}),)
        },
    )

    assert rows[0].evidence_status == "blocked"
    assert rows[0].selectable is False
    assert rows[0].golden_validation_scope is False
    assert rows[0].validation_case_parameters == {}
    assert rows[0].blocked_explanation is not None
    assert "cannot be represented" in rows[0].blocked_explanation


@pytest.mark.parametrize(
    ("parameter", "recorded_value"),
    (
        ("gap_bps", 0),
        ("gap_bps", False),
        ("rsi_min", True),
    ),
)
def test_golden_scope_rejects_pydantic_coercible_parameter_types(
    parameter: str,
    recorded_value: object,
) -> None:
    """A Golden scope is an exact serialized receipt, not a coerced request."""
    rows = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={
            "ema_crossover_signal": (_golden_scope(**{parameter: recorded_value}),)
        },
    )

    assert rows[0].evidence_status == "blocked"
    assert rows[0].selectable is False
    assert rows[0].golden_validation_scope is False


@pytest.mark.parametrize("recorded_symbol", ["missing", None, "tsla"])
def test_golden_scope_requires_a_canonical_symbol_in_its_exact_parameter_map(
    monkeypatch: pytest.MonkeyPatch,
    recorded_symbol: str | None,
) -> None:
    """The database scope and the deploy ticket must name the same canonical ticker."""
    strategy_key = "ema_crossover_signal"
    parameters = dict(_golden_scope().parameters)
    if recorded_symbol == "missing":
        parameters.pop("symbol")
    else:
        parameters["symbol"] = recorded_symbol
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({(strategy_key, ACCT)}),
    )

    row = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={
            strategy_key: (GoldenValidationScope(symbol="TSLA", parameters=parameters),)
        },
    )[0]

    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.golden_validation_scope is False


def test_golden_scope_with_a_hidden_parameter_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog cannot advertise a scope that deploy-time validation will reject."""
    strategy_key = "ema_crossover_signal"
    registration = _STRATEGY_REGISTRY[strategy_key]
    monkeypatch.setitem(
        _STRATEGY_REGISTRY,
        strategy_key,
        replace(registration, hidden_params=registration.hidden_params | {"gap"}),
    )
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({(strategy_key, ACCT)}),
    )

    row = _strategy_views(
        [],
        account_id=ACCT,
        custody_world="real_paper",
        golden_validation_scopes={strategy_key: (_golden_scope(gap=0.75),)},
    )[0]

    assert row.evidence_status == "blocked"
    assert row.selectable is False
    assert row.golden_validation_scope is False


def test_non_sealed_strategy_does_not_offer_a_paper_access_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _accepted_deploy_entry()
    registration = _STRATEGY_REGISTRY[entry.strategy_key]
    monkeypatch.setitem(
        _STRATEGY_REGISTRY,
        entry.strategy_key,
        replace(
            registration,
            signal_program_contract=None,
            signal_program_factory=None,
        ),
    )

    row = _strategy_views([entry], account_id=ACCT, custody_world="real_paper")[0]

    assert row.paper_access_state == "blocked"


def test_no_runtime_block_reads_differently_from_a_stale_proof_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1703/#1730: three distinct block reasons must read distinctly:
    no registered runtime, a stale accepted proof, and (new in #1730) a
    sealed Signal Program whose account is not canary-allowlisted."""
    stale_proof_entry = _accepted_deploy_entry().model_copy(update={"audit_copy_sha256": "0" * 64})
    no_runtime_entry = _accepted_deploy_entry()
    not_allowlisted_entry = _accepted_deploy_entry()

    monkeypatch.setattr(strategy_catalog, "supported_alpaca_paper_strategy_keys", lambda: frozenset())
    no_runtime_rows = _strategy_views([no_runtime_entry], account_id=ACCT, custody_world="real_paper")
    monkeypatch.undo()

    # ema_crossover_signal is a sealed Signal Program (#1730): without an
    # explicit canary admission, this account is blocked at the allowlist
    # before the accepted proof is ever re-verified. This test is about
    # stale-proof detection, not the allowlist, so admit this account for
    # the one pairing it needs to reach that check.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )
    stale_proof_rows = _strategy_views([stale_proof_entry], account_id=ACCT, custody_world="real_paper")
    monkeypatch.undo()

    # No canary admission at all: the same accepted entry now demotes to
    # the canary-not-allowlisted block instead of the accepted-proof block.
    not_allowlisted_rows = _strategy_views([not_allowlisted_entry], account_id=ACCT, custody_world="real_paper")

    no_runtime_reason = no_runtime_rows[0].blocked_explanation
    stale_proof_reason = stale_proof_rows[0].blocked_explanation
    not_allowlisted_reason = not_allowlisted_rows[0].blocked_explanation
    assert no_runtime_reason is not None
    assert stale_proof_reason is not None
    assert not_allowlisted_reason is not None
    assert len({no_runtime_reason, stale_proof_reason, not_allowlisted_reason}) == 3, (
        "no-runtime, stale-proof, and canary-not-allowlisted blocks must each read distinctly."
    )
    # A no-runtime row cannot even Dry Run; a stale-proof row still can, and
    # so does a canary-blocked row -- Dry Run is never canary-gated (#1730).
    assert no_runtime_rows[0].admissible_modes == ()
    assert stale_proof_rows[0].admissible_modes == ("dry_run",)
    assert not_allowlisted_rows[0].admissible_modes == ("dry_run",)


def test_selectable_rows_are_exactly_the_launchable_and_visible_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1703 replacement for the retired enum-to-runtime invariant test.

    Exhaustive sweep over every real committed, currently-validated
    registry entry, asserting the two halves of the acceptance criterion
    "whatever is selectable is launchable, and nothing the runner can
    execute is hidden":

    1. Every selectable row's strategy_key has a registered runtime.
    2. Every validated entry whose strategy_key has a registered runtime
       produces a row in the catalog — it is never silently dropped.

    #1730 AC: "Catalog selectability is exactly equivalent to
    launchability." Every real validated strategy today is a sealed
    Signal Program, so real launchability for Paper now also requires
    this account's exact (program, account) pairing to be canary-admitted
    (#1729). Both directions of that requirement are exercised below:
    unadmitted, every sealed-program entry with a runtime must be blocked
    (never wrongly selectable); admitted, at least one must become
    selectable again, proving the runtime/never-dropped sweep isn't
    vacuously satisfied by an allowlist that blocks everything.
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
    account_id = "strategy-catalog-sweep-account"

    unadmitted_rows = _strategy_views(validated_entries, account_id=account_id, custody_world="real_paper")
    for row in unadmitted_rows:
        registration = _STRATEGY_REGISTRY.get(row.strategy_key)
        is_sealed_program = registration is not None and registration.signal_program_contract is not None
        if is_sealed_program and row.strategy_key in runtime_keys:
            assert row.selectable is False, (
                f"{row.strategy_key} is a sealed Signal Program with a runtime, not "
                f"canary-admitted for '{account_id}', yet reports selectable=True."
            )

    # Admit this sweep's account for every entry under test so the
    # runtime/never-dropped invariants below are proven against a real
    # launchable case, not vacuously against an allowlist that blocks
    # everything.
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset((entry.strategy_key, account_id) for entry in validated_entries),
    )
    rows = _strategy_views(validated_entries, account_id=account_id, custody_world="real_paper")
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
    assert any(row.selectable for row in rows), (
        "fixture sanity: granting canary admission for every validated entry produced no "
        "selectable row; the runtime/never-dropped sweep above would pass vacuously."
    )
