"""Structural guardrails for the one-authority Alpaca lifespan wiring."""

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


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


def test_legacy_projection_is_explicitly_fenced_from_activated_sqlite() -> None:
    source = (REPOSITORY_ROOT / "PythonDataService/app/main.py").read_text(
        encoding="utf-8"
    )
    legacy_fence = source.index(
        'if alpaca_clerk_runtime.authority_kind == "legacy":'
    )
    projection_import = source.index(
        "from app.services.clerk_transaction_projection import",
        legacy_fence,
    )
    trade_updates_import = source.index(
        "from app.broker.alpaca.trade_updates import",
        projection_import,
    )

    assert legacy_fence < projection_import < trade_updates_import
