"""Structural guardrails for the sole SQLite Alpaca custody authority."""

import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPLICATION_ROOT = REPOSITORY_ROOT / "PythonDataService/app"
FRONTEND_ROOT = REPOSITORY_ROOT / "Frontend/src/app"
OPENAPI_PATH = REPOSITORY_ROOT / "contracts/openapi/python-data-service.openapi.json"

RETIRED_LEGACY_CUSTODY_PATHS = (
    "broker/alpaca/clerk/activity_recovery.py",
    "broker/alpaca/clerk/clerk.py",
    "broker/alpaca/clerk/custody.py",
    "broker/alpaca/clerk/custody_resolution.py",
    "broker/alpaca/clerk/custody_resolution_store.py",
    "broker/alpaca/clerk/decision_journal.py",
    "broker/alpaca/clerk/derive.py",
    "broker/alpaca/clerk/diagnosis.py",
    "broker/alpaca/clerk/effects.py",
    "broker/alpaca/clerk/exposure.py",
    "broker/alpaca/clerk/journal.py",
    "broker/alpaca/clerk/leg_identity.py",
    "broker/alpaca/clerk/reconcile.py",
    "broker/alpaca/clerk/recovery.py",
    "broker/alpaca/clerk/rollup_cache.py",
    "broker/alpaca/clerk/sweep.py",
    "engine/live/run_ledger.py",
    "services/broker_v2_panel/account_projection_owner.py",
)

RETIRED_FRONTEND_PATHS = (
    "components/brokers/alpaca-desk/alpaca-order-results.component.html",
    "components/brokers/alpaca-desk/alpaca-order-results.component.ts",
    "components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.html",
    "components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.scss",
    "components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.spec.ts",
    "components/brokers/alpaca-desk/custody-resolution-confirm-dialog.component.ts",
    "components/broker/v2-panel/panel-action-button/panel-action-comment-confirm.component.html",
    "components/broker/v2-panel/panel-action-button/panel-action-comment-confirm.component.scss",
    "components/broker/v2-panel/panel-action-button/panel-action-comment-confirm.component.spec.ts",
    "components/broker/v2-panel/panel-action-button/panel-action-comment-confirm.component.ts",
)


def test_main_selects_one_authority_and_has_no_additive_sqlite_writer() -> None:
    source = (REPOSITORY_ROOT / "PythonDataService/app/main.py").read_text(
        encoding="utf-8"
    )

    assert "select_active_clerk_runtime(" in source
    assert "set_active_clerk_runtime(alpaca_clerk_runtime)" in source
    assert "evidence_sink=alpaca_clerk_runtime.evidence_sink" in source
    assert "alpaca_clerk_runtime.sweep" in source
    assert "get_or_open_repository" not in source
    assert "sqlite_alpaca_sweep" not in source
    assert "SqliteReconciliationSweep" not in source


def test_legacy_custody_modules_and_selector_are_absent() -> None:
    for relative_path in RETIRED_LEGACY_CUSTODY_PATHS:
        assert not (APPLICATION_ROOT / relative_path).exists(), relative_path

    selector = (APPLICATION_ROOT / "broker/alpaca/clerk/active_authority.py").read_text(
        encoding="utf-8"
    )
    main = (APPLICATION_ROOT / "main.py").read_text(encoding="utf-8")
    assert "legacy_factory" not in selector
    assert 'authority_kind="legacy"' not in selector
    assert "_legacy_clerk" not in main
    assert "project_alpaca_journal_best_effort" not in main


def test_production_has_no_import_of_a_retired_legacy_module() -> None:
    production_sources = {
        path: path.read_text(encoding="utf-8")
        for path in APPLICATION_ROOT.rglob("*.py")
    }
    for relative_path in RETIRED_LEGACY_CUSTODY_PATHS:
        module = "app." + relative_path.removesuffix(".py").replace("/", ".")
        for path, source in production_sources.items():
            assert f"from {module} import" not in source, (path, module)
            assert f"import {module}" not in source, (path, module)


def test_legacy_broker_v2_mutation_dispatch_is_absent() -> None:
    broker_router = (APPLICATION_ROOT / "routers/brokers.py").read_text(encoding="utf-8")
    panel_source = (
        APPLICATION_ROOT / "services/broker_v2_panel/panel_data_source.py"
    ).read_text(encoding="utf-8")

    assert '@router.post(\n    "/{broker}/orders"' not in broker_router
    assert '@router.delete(\n    "/{broker}/orders/{order_id}"' not in broker_router
    assert '@router.post(\n    "/{broker}/clerk/clear-hold"' not in broker_router
    assert '@router.post(\n    "/{broker}/clerk/resolve"' not in broker_router
    assert '"clear_hold":' not in panel_source
    assert '"record_inventory_baseline":' not in panel_source


def test_frontend_has_no_legacy_custody_dispatch_or_orphaned_controls() -> None:
    for relative_path in RETIRED_FRONTEND_PATHS:
        assert not (FRONTEND_ROOT / relative_path).exists(), relative_path

    brokers_service = (FRONTEND_ROOT / "services/brokers.service.ts").read_text(
        encoding="utf-8"
    )
    for retired_method in (
        "submitOrder(",
        "cancelOrder(",
        "clearHold(",
        "resolveCustody(",
    ):
        assert retired_method not in brokers_service

    active_alpaca_components = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (FRONTEND_ROOT / "components/brokers/alpaca-desk").rglob("*.ts")
        if not path.name.endswith(".spec.ts")
    )
    for retired_dispatch in (
        ".submitOrder(",
        ".cancelOrder(",
        ".clearHold(",
        ".resolveCustody(",
        "kind: 'legacy'",
    ):
        assert retired_dispatch not in active_alpaca_components


def test_generated_contract_removes_generic_mutations_but_keeps_read_evidence() -> None:
    paths = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))["paths"]

    generic_orders = paths["/api/brokers/{broker}/orders"]
    assert set(generic_orders) == {"get"}
    assert "/api/brokers/{broker}/orders/{order_id}" not in paths
    assert "/api/brokers/{broker}/clerk/clear-hold" not in paths
    assert "/api/brokers/{broker}/clerk/resolve" not in paths
    assert "get" in paths["/api/brokers/{broker}/clerk/status"]
    assert "get" in paths["/api/brokers/{broker}/clerk/custody-diagnosis"]


def test_generated_contract_preserves_sqlite_custody_routes() -> None:
    paths = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))["paths"]
    required_operations = {
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{strategy_instance_id}/runs/start": "post",
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/bots/{strategy_instance_id}/runs/stop": "post",
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/reconcile": "post",
        "/api/alpaca-clerk-sqlite/accounts/{account_id}/timeline": "get",
        "/api/brokers/alpaca/accounts/{account_id}/manual-orders/preview": "post",
        "/api/brokers/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}": "put",
        "/api/brokers/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}/continue": "post",
        "/api/brokers/alpaca/accounts/{account_id}/manual-order-tickets/{ticket_id}/cancel": "post",
    }
    for path, method in required_operations.items():
        assert method in paths[path], (method, path)


def test_ibkr_history_and_strict_historical_identity_reads_are_preserved() -> None:
    transaction_projection = (
        APPLICATION_ROOT / "services/clerk_transaction_projection.py"
    ).read_text(encoding="utf-8")
    assert "AccountClerkJournalEntry" in transaction_projection
    assert "ACCOUNT_CLERK_JOURNAL_FILENAME" in transaction_projection
    assert "project_alpaca" not in transaction_projection

    strict_reader_path = APPLICATION_ROOT / "engine/live/historical_run_identity.py"
    strict_reader = strict_reader_path.read_text(encoding="utf-8")
    assert "read_historical_strategy_instance_id" in strict_reader
    assert "path.is_symlink() or not path.is_file()" in strict_reader
    for writer_symbol in ("write_text", "write_bytes", ".open(\"w", ".open(\"a"):
        assert writer_symbol not in strict_reader

    identity_guard = (APPLICATION_ROOT / "services/alpaca_bot_identity.py").read_text(
        encoding="utf-8"
    )
    assert "read_historical_strategy_instance_id" in identity_guard
    assert "live_runs_root.is_symlink()" in identity_guard
    assert "run_dir.is_symlink()" in identity_guard


def test_alpaca_models_and_reads_have_no_legacy_custody_fallback() -> None:
    models = (APPLICATION_ROOT / "broker/alpaca/clerk/models.py").read_text(
        encoding="utf-8"
    )
    transaction_router = (APPLICATION_ROOT / "routers/clerk_transactions.py").read_text(
        encoding="utf-8"
    )

    for retired_symbol in (
        "AccountClerkBrokerEvidenceBaseline",
        "BROKER_EVIDENCE_BASELINE",
        "OrderSubmitResult",
        "OrderCancelResult",
    ):
        assert retired_symbol not in models
    assert 'if broker == "ibkr":' in transaction_router
    assert 'Literal["alpaca", "ibkr"]' in transaction_router
    assert "Activated SQLite Clerk transaction history is unavailable" in transaction_router
    assert "Activated SQLite Clerk transaction detail is unavailable" in transaction_router


def test_migration_gate_requires_an_explicit_nonempty_inventory() -> None:
    gate = (
        APPLICATION_ROOT
        / "broker/alpaca/clerk/sqlite/activation_inventory.py"
    ).read_text(encoding="utf-8")
    cli = (
        REPOSITORY_ROOT
        / "PythonDataService/scripts/qualify_alpaca_activation_inventory.py"
    ).read_text(encoding="utf-8")

    assert "at least one in-use account" in gate
    assert "store.resolve(" in gate
    assert "verify_database(" in gate
    assert "--inventory" in cli
    assert "--output" in cli


def test_every_reconciliation_sweep_publishes_its_verdict() -> None:
    """A sweep that does not publish makes pure panel reads permanently stale.

    ``ReconciliationSweep`` is the sole automatic reconciler, and it calls
    ``reconcile_account`` directly rather than through the facade. Panel reads
    project the facade's last verdict, so a construction site that omits
    ``on_result`` leaves ``custody_snapshot_projection`` answering ``stale``
    with ``reconciled_at_ms=0`` however many sweeps succeed (#1776 WP2).

    Structural because the failure is silent: every unit test still passes and
    the degradation only shows in production.
    """
    source = (
        APPLICATION_ROOT / "broker/alpaca/clerk/active_authority.py"
    ).read_text(encoding="utf-8")

    constructions = source.count("ReconciliationSweep(")
    publishers = source.count("on_result=facade.publish_reconciliation")

    assert constructions > 0, "no sweep construction found; update this guard"
    assert publishers == constructions, (
        f"{constructions} ReconciliationSweep construction(s) but {publishers} "
        "publish their verdict; every sweep must feed the read projection"
    )


def test_the_stream_health_hold_sync_is_started_by_the_real_authority() -> None:
    """An unstarted hold sync is worse than none at all (#1777 WP4).

    ENTER no longer writes the durable stream-health hold -- it only
    refuses. If nothing starts ``StreamHealthHoldSync``, the account-scoped
    record is never raised for a genuine outage and never released after
    one, and no unit test notices: every entry-time refusal still works.

    Structural for the same reason as the sweep guard above: the failure is
    a missing call, and it is silent.
    """
    source = (
        APPLICATION_ROOT / "broker/alpaca/clerk/active_authority.py"
    ).read_text(encoding="utf-8")

    constructions = source.count("StreamHealthHoldSync(")
    starts = source.count("hold_sync.start()")
    stops = source.count("await hold_sync.stop()")

    assert constructions > 0, "no hold sync construction found; update this guard"
    assert starts == constructions, (
        f"{constructions} StreamHealthHoldSync construction(s) but {starts} "
        "started; an unstarted sync never raises or releases the hold"
    )
    assert stops >= constructions, (
        "every started hold sync must be stopped on shutdown and on a failed "
        "startup, or its task outlives the repository it writes to"
    )


def test_enter_does_not_write_the_account_scoped_stream_health_hold() -> None:
    """Finding S10, pinned structurally (#1777 WP4).

    The hold is account-scoped and gates admission for every bot on the
    account. Raising it from one bot's entry -- on a single sample, with no
    debounce -- is what froze the whole account on a 5 s blip. The entry
    path may refuse; only the background sync may write the record.
    """
    source = (
        APPLICATION_ROOT / "broker/alpaca/clerk/sqlite/runtime.py"
    ).read_text(encoding="utf-8")

    assert "observe_account_hold(" not in source, (
        "runtime.py raises an account hold again; the stream-health hold "
        "belongs to StreamHealthHoldSync, not to ENTER (#1777 WP4/S10)"
    )
