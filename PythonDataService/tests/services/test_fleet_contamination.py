"""Journal-canonical fleet contamination seams for issue #1024."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.fleet_contamination as fleet_contamination
from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    ACCOUNT_EVENTS_FILENAME,
    AccountFreezeEvidence,
    append_account_event,
    read_account_events,
    read_account_freeze,
    write_account_freeze,
)
from app.engine.live.account_clerk import AccountClerk
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    write_account_instance_binding,
)
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.order_identity import build_order_ref
from app.services.account_journal_authority import (
    _AccountJournalAuthorityState,
    _has_requalification_window,
    _JournalParityObservation,
    _qualification_alarm_is_active,
    _read_authority_state,
    _write_authority_state,
    account_journal_authority_is_active,
    record_account_journal_stream_state,
)
from app.services.fleet_contamination import (
    collect_fleet_position_explanations,
    instance_broker,
    record_account_journal_parity_observation,
)


def test_instance_broker_uses_clerk_positions_not_stale_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = SimpleNamespace(
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        expected_position_by_symbol={"QQQ": 1},
        pending_intents=[],
    )
    monkeypatch.setattr(
        fleet_contamination,
        "read_instance_live_state",
        lambda _root, _sid: envelope,
    )
    monkeypatch.setattr(
        fleet_contamination,
        "_account_id_for_run",
        lambda _root, _run_id: "DU123456",
    )
    monkeypatch.setattr(
        fleet_contamination,
        "_collect_journal_position_explanations",
        lambda _root, *, account_id=None: {},
    )

    broker = instance_broker(tmp_path / "live_runs", "bot-a")

    assert broker is not None
    assert broker.owned_positions == {}
    assert broker.pending_order_count == 0


def test_instance_broker_treats_stale_sidecar_as_unknown_when_ledger_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken ledger cannot authorize attribution from a local sidecar."""

    envelope = SimpleNamespace(
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        expected_position_by_symbol={"QQQ": 1},
        pending_intents=[],
    )
    monkeypatch.setattr(
        fleet_contamination,
        "read_instance_live_state",
        lambda _root, _sid: envelope,
    )

    monkeypatch.setattr(fleet_contamination, "_account_id_for_run", lambda _root, _run_id: None)

    assert instance_broker(tmp_path / "live_runs", "bot-a") is None


@pytest.mark.parametrize("run_id", ("../other-run", "run-a/../other-run"))
def test_instance_broker_rejects_sidecar_run_id_outside_live_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    envelope = SimpleNamespace(
        strategy_instance_id="bot-a",
        run_id=run_id,
        bot_order_namespace="learn-ai/bot-a/v1",
        expected_position_by_symbol={"QQQ": 1},
        pending_intents=[],
    )
    monkeypatch.setattr(fleet_contamination, "read_instance_live_state", lambda _root, _sid: envelope)

    assert instance_broker(tmp_path / "live_runs", "bot-a") is None


def test_instance_broker_rejects_mismatched_legacy_ledger_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "live_runs"
    run_dir = root / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps({"run_id": "other-run", "account_id": "DU123456"}),
        encoding="utf-8",
    )
    envelope = SimpleNamespace(
        strategy_instance_id="bot-a",
        run_id="run-a",
        bot_order_namespace="learn-ai/bot-a/v1",
        expected_position_by_symbol={"QQQ": 1},
        pending_intents=[],
    )
    monkeypatch.setattr(fleet_contamination, "read_instance_live_state", lambda _root, _sid: envelope)

    assert instance_broker(root, "bot-a") is None


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
    assert not any(
        event["event_type"].startswith("account_clerk_sidecar_journal")
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


def test_account_net_fetch_mismatch_blocks_account_fleet_starts(monkeypatch, tmp_path: Path) -> None:
    class _Snapshot:
        account_id = "DU-OTHER"
        positions = []

    class _Account:
        async def fetch_positions(self, _client):
            return _Snapshot()

    monkeypatch.setattr("app.broker.ibkr.account.fetch_positions", _Account().fetch_positions)
    monkeypatch.setattr("app.routers.broker_dependencies.require_connected_client", lambda: object())

    contamination = asyncio.run(
        fleet_contamination.compute_account_fleet_contamination(
            tmp_path / "live_runs",
            account_id="DU-EXPECTED",
        )
    )

    assert contamination.verdict == "unknown"
    assert contamination.policy_blocks_starts is True
    assert "mismatches" in contamination.summary


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

    assert collect_fleet_position_explanations(tmp_path / "live_runs") == {"bot-a": {"SPY": 1}}
    assert read_account_events(tmp_path, account) == []


def test_explicit_parity_observer_writes_only_for_requested_account(tmp_path: Path, monkeypatch) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", lambda _root, **_kw: {})
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root, **_kw: {})

    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is False
    [event] = read_account_events(tmp_path, account)
    assert event["event_type"] == "account_clerk_sidecar_journal_parity"


def test_forward_producer_display_event_cannot_qualify_journal_authority(tmp_path: Path) -> None:
    account = "DU123456"
    append_account_event(
        tmp_path,
        account,
        {"event_type": "account_clerk_journal_authority_requalified", "ts_ms": 1},
    )

    assert account_journal_authority_is_active(tmp_path, account) is False


def test_legacy_shadow_comparator_drops_zero_position_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = type(
        "Envelope",
        (),
        {
            "run_id": "run-a",
            "bot_order_namespace": "learn-ai/bot-a/v1",
            "expected_position_by_symbol": {"SPY": 0},
        },
    )()
    monkeypatch.setattr(fleet_contamination, "scan_runs_by_instance", lambda _root: {"bot-a": []})
    monkeypatch.setattr(fleet_contamination, "read_instance_live_state", lambda _root, _sid: envelope)
    monkeypatch.setattr(fleet_contamination, "_retired_claim_keys_for_run", lambda **_kwargs: frozenset())

    assert fleet_contamination._collect_legacy_fleet_position_explanations(
        tmp_path / "live_runs"
    ) == {}


def test_legacy_cutover_is_invalidated_once_before_new_shadow_observations(tmp_path: Path, monkeypatch) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "accounts" / account / "account_events.jsonl").write_text(
        json.dumps(
            {
                "account_id": account,
                "event_type": "account_clerk_journal_authority_cutover",
                "ts_ms": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", lambda _root, **_kw: {})
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root, **_kw: {})

    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is False
    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is False

    event_types = [event["event_type"] for event in read_account_events(tmp_path, account)]
    assert event_types.count("account_clerk_journal_authority_requalification_required") == 1
    assert "account_clerk_journal_authority_requalified" not in event_types


def test_requalification_requires_fifteen_minutes_ten_observations_and_nonzero_to_zero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    clock = {"ms": 1_700_000_000_000}
    monkeypatch.setattr(fleet_contamination.time, "time_ns", lambda: clock["ms"] * 1_000_000)

    def explained(_root: Path, **_kw) -> dict[str, dict[str, int]]:
        return {"bot-a": {"SPY": 1}} if clock["ms"] == 1_700_000_000_000 else {}

    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", explained)
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", explained)

    for observation in range(16):
        assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is (observation == 15)
        clock["ms"] += 60_000

    events = read_account_events(tmp_path, account)
    qualified = [event for event in events if event["event_type"] == "account_clerk_journal_authority_requalified"]
    assert len(qualified) == 1
    assert [event for event in events if event["event_type"] == "account_clerk_sidecar_journal_parity"][-1]["journal_nonzero"] is False


def test_parity_observer_throttles_per_account_and_alarm_resets_qualification_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    clock = {"ms": 1_700_000_000_000}
    monkeypatch.setattr(fleet_contamination.time, "time_ns", lambda: clock["ms"] * 1_000_000)
    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", lambda _root, **_kw: {"bot-a": {"SPY": 1}})
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root, **_kw: {"bot-a": {"SPY": 1}})

    record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account)
    record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account)
    assert len(read_account_events(tmp_path, account)) == 1

    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id=account,
            reason="test.alarm",
            source="test",
            recorded_at_ms=clock["ms"],
            operator_next_step="CHECK_IBKR",
        ),
    )
    clock["ms"] += 60_000
    record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account)
    parity = [event for event in read_account_events(tmp_path, account) if event["event_type"] == "account_clerk_sidecar_journal_parity"]
    assert [event["status"] for event in parity] == ["clean", "drift"]


def test_post_cutover_drift_creates_an_account_operator_condition(tmp_path: Path, monkeypatch) -> None:
    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    _write_authority_state(
        tmp_path,
        _AccountJournalAuthorityState(account_id=account, qualified_at_ms=1),
    )
    monkeypatch.setattr(fleet_contamination, "_collect_journal_position_explanations", lambda _root, **_kw: {"bot-a": {"SPY": 1}})
    monkeypatch.setattr(fleet_contamination, "_collect_legacy_fleet_position_explanations", lambda _root, **_kw: {})

    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is False

    event_types = [event["event_type"] for event in read_account_events(tmp_path, account)]
    assert "account_clerk_journal_authority_drift_detected" in event_types
    assert read_account_freeze(tmp_path, account) is not None


def test_post_cutover_state_change_bypasses_background_parity_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean sample must not defer a newly observed unsafe account state."""

    account = "DU123456"
    (tmp_path / "accounts" / account).mkdir(parents=True)
    (tmp_path / "accounts" / account / "clerk_journal.jsonl").write_text("", encoding="utf-8")
    _write_authority_state(
        tmp_path,
        _AccountJournalAuthorityState(account_id=account, qualified_at_ms=1),
    )
    clock = {"ms": 1_700_000_000_000}
    monkeypatch.setattr(fleet_contamination.time, "time_ns", lambda: clock["ms"] * 1_000_000)
    monkeypatch.setattr(
        fleet_contamination,
        "_collect_journal_position_explanations",
        lambda _root, **_kw: {},
    )
    monkeypatch.setattr(
        fleet_contamination,
        "_collect_legacy_fleet_position_explanations",
        lambda _root, **_kw: {},
    )

    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is True
    write_account_freeze(
        tmp_path,
        AccountFreezeEvidence(
            account_id=account,
            reason="test.new_alarm",
            source="test",
            recorded_at_ms=clock["ms"],
            operator_next_step="CHECK_IBKR",
        ),
    )

    assert record_account_journal_parity_observation(tmp_path / "live_runs", account_id=account) is False
    parity = [
        event
        for event in read_account_events(tmp_path, account)
        if event["event_type"] == "account_clerk_sidecar_journal_parity"
    ]
    assert [event["status"] for event in parity] == ["clean", "drift"]
    assert parity[-1]["reason"] == "ACCOUNT_CLERK_UNRESOLVED_ALARM"
    assert parity[-1]["trigger"] == "state_change"
    assert read_account_freeze(tmp_path, account) is not None


def test_cleared_alarm_still_restarts_the_requalification_window() -> None:
    observations = tuple(
        _JournalParityObservation(
            observed_at_ms=2_000_000 + (offset * 100_000),
            status="clean",
            reason="SIDECAR_JOURNAL_EXPOSURE_MATCH",
            fingerprint=f"clean-{offset}",
            journal_nonzero=offset == 0,
        )
        for offset in range(9)
    )
    authority = _AccountJournalAuthorityState(
        account_id="DU123456",
        requalification_required_at_ms=1_900_000,
        observations=observations,
    )
    assert _has_requalification_window(authority) is False

    authority = authority.model_copy(
        update={
            "observations": (
                *authority.observations,
                _JournalParityObservation(
                    observed_at_ms=2_900_000,
                    status="clean",
                    reason="SIDECAR_JOURNAL_EXPOSURE_MATCH",
                    fingerprint="clean-9",
                    journal_nonzero=False,
                ),
            )
        }
    )
    assert _has_requalification_window(authority) is True


def test_recovered_event_stream_no_longer_blocks_requalification(tmp_path: Path) -> None:
    account = "DU123456"
    record_account_journal_stream_state(
        tmp_path,
        account,
        state="down",
        changed_at_ms=1,
    )
    assert _qualification_alarm_is_active(tmp_path, account, _read_authority_state(tmp_path, account)) is True

    record_account_journal_stream_state(
        tmp_path,
        account,
        state="recovered",
        changed_at_ms=2,
    )

    assert _qualification_alarm_is_active(tmp_path, account, _read_authority_state(tmp_path, account)) is False


def test_legacy_callback_stream_down_revokes_snapshotted_journal_authority(tmp_path: Path) -> None:
    account = "DU123456"
    legacy_path = tmp_path / "accounts" / account / ACCOUNT_EVENTS_FILENAME
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {
                    "event_type": "account_clerk_journal_authority_requalified",
                    "ts_ms": 1_700_000_000_000,
                },
                {
                    "event_type": "account_clerk_event_stream_down",
                    "ts_ms": 1_700_000_001_000,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert account_journal_authority_is_active(tmp_path, account) is False
    state = _read_authority_state(tmp_path, account)
    assert state.event_stream_state == "down"
    assert state.event_stream_changed_at_ms == 1_700_000_001_000
    assert state.requalification_required_at_ms == 1_700_000_001_000


def test_malformed_legacy_requalification_timestamp_cannot_grant_authority(tmp_path: Path) -> None:
    account = "DU123456"
    legacy_path = tmp_path / "accounts" / account / ACCOUNT_EVENTS_FILENAME
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "event_type": "account_clerk_journal_authority_requalified",
                "ts_ms": "not-a-timestamp",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert account_journal_authority_is_active(tmp_path, account) is False


def test_legacy_journal_authority_is_snapshotted_once_before_a_live_decision(tmp_path: Path) -> None:
    account = "DU123456"
    legacy_path = tmp_path / "accounts" / account / ACCOUNT_EVENTS_FILENAME
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "event_type": "account_clerk_journal_authority_requalified",
                "ts_ms": 1_700_000_000_000,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert account_journal_authority_is_active(tmp_path, account) is True
    legacy_path.write_text("{truncated", encoding="utf-8")

    assert account_journal_authority_is_active(tmp_path, account) is True
