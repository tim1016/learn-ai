"""Journal-canonical fleet contamination seams for issue #1024."""

from __future__ import annotations

import asyncio
from pathlib import Path

import app.services.fleet_contamination as fleet_contamination
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import read_account_events
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.order_identity import build_order_ref
from app.services.fleet_contamination import collect_fleet_position_explanations


def test_journal_exposure_is_canonical(tmp_path: Path, monkeypatch) -> None:
    account = "DU123456"
    sid = "bot-a"
    namespace = bot_order_namespace_for_instance(sid)
    write_account_instance_binding(
        tmp_path,
        AccountInstanceBinding(
            account_id=account,
            strategy_instance_id=sid,
            run_id="run-a",
            bot_order_namespace=namespace,
            lifecycle_state="ACTIVE",
            recorded_at_ms=1,
            source="test",
        ),
    )
    intent = AccountOwnerSubmitIntent(
        trace_id="trace-a",
        account_id=account,
        strategy_instance_id=sid,
        run_id="run-a",
        bot_order_namespace=namespace,
        intent_id="intent-a",
        order_ref=build_order_ref(namespace, "intent-a"),
        intent_kind="STRATEGY",
        order_spec=IbkrOrderSpec(
            symbol="SPY", sec_type="STK", action="BUY", quantity=2,
            order_type="MKT", confirm_paper=True,
            order_ref=build_order_ref(namespace, "intent-a"),
        ).model_dump(),
        owner_generation=1,
        created_at_ms=1,
    )
    clerk = AccountClerk(artifacts_root=tmp_path, account_id=account)
    asyncio.run(clerk.record_intent(intent))
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root: {})
    assert collect_fleet_position_explanations(tmp_path / "live_runs") == {}

    clerk.append_broker_event(intent, IbkrOrderEvent(
        account_id=account, order_id=1, event_type="fill", order_ref=intent.order_ref,
        symbol="SPY", side="BUY", fill_quantity=2, exec_id="exec-a", ts_ms=2,
    ))

    expected = {sid: {"SPY": 2}}
    legacy = {"positions": expected}
    monkeypatch.setattr(
        fleet_contamination,
        "_collect_legacy_fleet_position_explanations",
        lambda _root: legacy["positions"],
    )
    for _ in range(3):
        explained = collect_fleet_position_explanations(tmp_path / "live_runs")
    assert explained == expected
    legacy["positions"] = {sid: {"SPY": 99}}
    assert collect_fleet_position_explanations(tmp_path / "live_runs") == expected
    assert any(
        event["event_type"] == "account_clerk_journal_authority_cutover"
        for event in read_account_events(tmp_path, account)
    )
    assert compute_fleet_contamination({"SPY": 2}, explained)["verdict"] == "clean"
    assert compute_fleet_contamination({"SPY": 1}, explained)["summary"].startswith("Managed bot artifacts overstate")
    assert compute_fleet_contamination({"SPY": 3}, explained)["summary"].startswith("Unmanaged broker position")


def test_account_fleet_computation_scopes_journal_reads_to_requested_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[str | None] = []

    async def positions() -> dict[str, int]:
        return {}

    def explanations(_root: Path, *, account_id: str | None = None) -> dict[str, dict[str, int]]:
        seen.append(account_id)
        return {}

    monkeypatch.setattr(fleet_contamination, "collect_fleet_position_explanations", explanations)

    asyncio.run(
        fleet_contamination.compute_account_fleet_contamination(
            tmp_path / "live_runs",
            positions,
            account_id="DU-A",
        )
    )

    assert seen == ["DU-A"]


def test_account_scoped_journals_cannot_cross_net_offsetting_exposure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def explanations(_root: Path, *, account_id: str | None = None) -> dict[str, dict[str, int]]:
        assert account_id is not None
        return {
            "DU-A": {"bot-a": {"SPY": 3}},
            "DU-B": {"bot-b": {"SPY": -3}},
        }[account_id]

    monkeypatch.setattr(fleet_contamination, "collect_fleet_position_explanations", explanations)

    async def long_account_positions() -> dict[str, int]:
        return {"SPY": 3}

    async def short_account_positions() -> dict[str, int]:
        return {"SPY": -3}

    long_account = asyncio.run(
        fleet_contamination.compute_account_fleet_contamination(
            tmp_path / "live_runs",
            long_account_positions,
            account_id="DU-A",
        )
    )
    short_account = asyncio.run(
        fleet_contamination.compute_account_fleet_contamination(
            tmp_path / "live_runs",
            short_account_positions,
            account_id="DU-B",
        )
    )

    assert long_account.verdict == "clean"
    assert short_account.verdict == "clean"
    assert long_account.explained_total == {"SPY": 3}
    assert short_account.explained_total == {"SPY": -3}


def test_broker_fetch_failure_is_an_account_scoped_start_blocker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        fleet_contamination,
        "collect_fleet_position_explanations",
        lambda _root, *, account_id=None: {},
    )

    async def unavailable_positions() -> None:
        return None

    result = asyncio.run(
        fleet_contamination.compute_account_fleet_contamination(
            tmp_path / "live_runs",
            unavailable_positions,
            account_id="DU-A",
        )
    )

    assert result.verdict == "unknown"
    assert result.policy_blocks_starts is True


def test_shadow_drift_keeps_legacy_authoritative_and_emits_alarm(tmp_path: Path, monkeypatch) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", lambda _root: {"bot-a": {"SPY": 1}})
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root: {})

    assert collect_fleet_position_explanations(tmp_path / "live_runs") == {}

    [event] = read_account_events(tmp_path, account)
    assert event["event_type"] == "account_clerk_sidecar_journal_parity"
    assert event["status"] == "drift"
