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
    clerk.append_broker_event(intent, IbkrOrderEvent(
        account_id=account, order_id=1, event_type="fill", order_ref=intent.order_ref,
        symbol="SPY", side="BUY", fill_quantity=2, exec_id="exec-a", ts_ms=2,
    ))

    expected = {sid: {"SPY": 2}}
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root: expected)
    for _ in range(3):
        explained = collect_fleet_position_explanations(tmp_path / "live_runs")
    assert explained == expected
    assert compute_fleet_contamination({"SPY": 2}, explained)["verdict"] == "clean"
    assert compute_fleet_contamination({"SPY": 1}, explained)["summary"].startswith("Managed bot artifacts overstate")
    assert compute_fleet_contamination({"SPY": 3}, explained)["summary"].startswith("Unmanaged broker position")


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
