"""Regression coverage for the post-activation Broker V2 roster source."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.broker_v2_panel import panel_data_source, sqlite_panel_source


class _Repository:
    def strategy_instances(self) -> list[dict[str, object]]:
        return [
            {
                "strategy_instance_id": "active-spy",
                "symbol": "SPY",
                "config_hash": "a" * 64,
                "created_at_ms": 1_700_000_000_000,
                "retired_at_ms": None,
            },
            {
                "strategy_instance_id": "retired-qqq",
                "symbol": "QQQ",
                "config_hash": "b" * 64,
                "created_at_ms": 1_600_000_000_000,
                "retired_at_ms": 1_710_000_000_000,
            },
        ]

    def active_run(self, strategy_instance_id: str):
        if strategy_instance_id == "active-spy":
            return SimpleNamespace(
                lifecycle_run_id="run-active",
                started_at_ms=1_720_000_000_000,
                stopped_at_ms=None,
            )
        return None


def test_sqlite_roster_uses_only_activated_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    facade = SimpleNamespace(account_id="paper-account", repository=_Repository())
    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: facade)

    statuses = sqlite_panel_source.read_sqlite_roster_statuses("alpaca")

    assert statuses is not None
    assert [status.strategy_instance_id for status in statuses] == ["active-spy", "retired-qqq"]
    assert statuses[0].running is True
    assert statuses[0].active_run_id == "run-active"
    assert statuses[0].phase == "ON_DUTY"
    assert statuses[0].strategy_key == "unknown"
    assert statuses[0].quantity is None
    assert statuses[1].running is False
    assert statuses[1].phase == "RETIRED"


@pytest.mark.asyncio
async def test_catalog_does_not_scan_runner_bindings_after_sqlite_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = sqlite_panel_source.read_sqlite_roster_statuses
    monkeypatch.setattr(panel_data_source, "_validate_account", _resolved_account)
    monkeypatch.setattr(panel_data_source, "read_sqlite_roster_statuses", statuses)
    monkeypatch.setattr(
        panel_data_source,
        "_bot_statuses",
        lambda _broker: pytest.fail("activated catalog scanned file-backed runner bindings"),
    )
    monkeypatch.setattr(
        panel_data_source,
        "_latest_decision",
        lambda *_args: pytest.fail("activated catalog read legacy decision journals"),
    )
    monkeypatch.setattr(
        panel_data_source,
        "get_or_create_owner",
        lambda *_args: pytest.fail("activated catalog used the legacy rollup owner"),
    )
    monkeypatch.setattr(panel_data_source, "read_sqlite_catalog_projections", _empty_projections)
    monkeypatch.setattr(
        sqlite_panel_source,
        "active_sqlite_facade",
        lambda _broker: SimpleNamespace(account_id="paper-account", repository=_Repository()),
    )

    result = await panel_data_source.get_catalog("alpaca", "paper-account")

    assert [row.strategy_instance_id for row in result] == ["active-spy", "retired-qqq"]


async def _resolved_account(_broker: str, account_id: str) -> str:
    return account_id


async def _empty_projections(
    _broker: str,
    _account_id: str,
    _sids: list[str],
) -> dict[str, object]:
    return {}
