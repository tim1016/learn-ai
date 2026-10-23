"""Retained failure diagnosis never replays, captures, or consults a mutable lake (#3399)."""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.session_result import PinnedDay
from app.research import options_candidate_evidence as reader
from app.research import options_failure_evidence as failures
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import stages
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.marks import MARK_RULE_V1, MARK_RULE_V2
from app.research.options_golden_search.protocol import OptionsSearchProtocol, Window
from app.research.options_golden_search.snapshots import Snapshot
from app.routers.options_failure_evidence import router
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import ny_datetime
from tests.research.options_golden_search._evidence import as_a_new_linked_study_under_mark_rule_v2, scene

FIXTURES = Path(__file__).parents[2] / "fixtures" / "options_evidence"


@pytest.fixture
def retained(tmp_path: Path) -> tuple[reader.RetainedOptionsEvidence, dict[str, Any], BlobStore]:
    row = json.loads((FIXTURES / "study-record.json").read_text())
    data = json.loads((FIXTURES / "development-evidence.json").read_text())
    blobs = BlobStore(tmp_path)
    assert blobs.put(json.dumps(data["payload"], separators=(",", ":"), allow_nan=False).encode()) == data["record"]["result"]["evidence_blob"]
    digest = reader.setup_sha256(reader.OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    return reader.load_record(row, data["record"], digest, "development", blobs=blobs), row, blobs


def test_real_missing_marks_remain_unknown_despite_actual_closing_fill(retained: tuple) -> None:
    evidence, row, _blobs = retained
    view = failures.project(evidence, row, offset=1, limit=1)
    assert (view.replayed_sessions, view.scheduled_sessions, view.traded_sessions) == (4, 373, 2)
    assert (view.required_held_marks, view.present_held_marks, view.missing_held_marks) == (599, 597, 2)
    assert (view.eligible_fold_winners, view.forward_fold_count, view.final_exposure) == (0, 6, "not_opened")
    assert not view.risk_complete and view.total == 2
    assert view.items[0].at_ms == 1730925600000
    assert view.items[0].close_filled_at_ms == 1730925960000
    assert view.items[0].contract == "SPY241106P00588000"
    assert "Cause not established" in view.items[0].explanation
    assert view.items[0].expected_input_sha256 is not None


def test_v2_values_the_real_gaps_so_no_missing_mark_event_remains_and_the_carried_marks_are_disclosed(
    retained: tuple, tmp_path: Path,
) -> None:
    _evidence, row, blobs = retained
    data = json.loads((FIXTURES / "development-evidence.json").read_text())
    linked, record = as_a_new_linked_study_under_mark_rule_v2(row, data["record"], blobs)
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(row["protocol"]).setup)
    evidence = reader.load_record(linked, record, digest, "development", blobs=blobs)
    view = failures.project(evidence, linked)
    assert (view.mark_rule, view.carried_held_marks, view.longest_carried_run) == (MARK_RULE_V2, 2, 1)
    assert (view.required_held_marks, view.present_held_marks, view.missing_held_marks) == (599, 597, 0)
    assert view.present_held_marks + view.carried_held_marks + view.missing_held_marks == view.required_held_marks
    assert view.items == [] and view.total == 0
    assert not any("missing_held_mark" in reason for reason in view.technical_failures)
    with pytest.raises(LookupError, match="not in the retained evaluation"):
        failures.inspect_record(evidence, linked, evidence.result.sessions[-1].session_open_ms, 1730925480000, "put_atm")
    # The original study still reports its two gaps as missing under its own rule.
    original = failures.project(reader.load_record(row, data["record"], digest, "development", blobs=blobs), row)
    assert (original.mark_rule, original.carried_held_marks, original.missing_held_marks) == (MARK_RULE_V1, 0, 2)


def test_unclosed_position_is_not_a_closing_fill(retained: tuple) -> None:
    evidence, row, _blobs = retained
    day = et_date_at_ms(evidence.result.sessions[0].session_open_ms)
    clock = session_window_for_date(day)
    written, data = scene((day,), exits=("2",), missing_minute=61)
    window = Window(key="development", start_ms=clock.open_ms_utc, end_ms=clock.close_ms_utc + 1,
        session_ms=(clock.open_ms_utc,), annualization_sessions=251, activity_floor=1)
    result = evaluate_setup(written, window, data)
    evidence = replace(evidence, setup=written, window=window, result=result)
    view = failures.project(evidence, row, offset=len(failures.events(evidence)) - 1)
    unclosed = next(item for item in view.items if item.code == "unclosed_position")
    assert unclosed.close_filled_at_ms is None
    assert unclosed.at_ms == clock.close_ms_utc
    assert result.sessions[0].outcome.status.value == "incomplete"


def with_input(evidence: reader.RetainedOptionsEvidence, row: dict[str, Any], blobs: BlobStore,
               *, contains_bar: bool = False, unreadable: bool = False) -> tuple[reader.RetainedOptionsEvidence, dict[str, Any]]:
    failure = failures.events(evidence)[0]
    session = evidence.result.sessions[-1]
    contract = next(mark.contract for point in session.observations for mark in point.held if mark.close is None)
    day = et_date_at_ms(session.session_open_ms)
    bars = [MinuteTradeBar(bar_start_et=ny_datetime(failure.at_ms - 60_000), open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(1))] if contains_bar else []
    payload = b"not a zip" if unreadable else build_minute_trade_zip_bytes(contract.occ_symbol, f"{day:%Y%m%d}", bars)
    digest = blobs.put(payload)
    snapshot = Snapshot(manifest={failure.retained_path: digest}, absent=(), retained_bytes=len(payload))
    pins = replace(evidence.result.pins[-1], option_days=(PinnedDay(contract, digest),))
    result = replace(evidence.result, pins=(*evidence.result.pins[:-1], pins))
    return replace(evidence, result=result, protocol=evidence.protocol.model_copy(update={"snapshot_id": snapshot.snapshot_id})), {**row, "snapshot": snapshot.model_dump(mode="json")}


@pytest.mark.parametrize("contains_bar", [False, True])
def test_targeted_verified_input_distinguishes_sparse_bars_from_trace_disagreement(retained: tuple, contains_bar: bool) -> None:
    evidence, row, blobs = retained
    evidence, row = with_input(evidence, row, blobs, contains_bar=contains_bar)
    point = failures.events(evidence)[0]
    inspected = failures.inspect_record(evidence, row, point.session_open_ms, point.at_ms, point.leg_id, blobs=blobs)
    assert inspected.code == ("retained_mark_input_disagreement" if contains_bar else "no_eligible_trade_bar")
    assert inspected.input_verified and inspected.contains_eligible_trade_bar is contains_bar


@pytest.mark.parametrize("cause", ["missing", "corrupt", "unreadable", "changed"])
def test_targeted_input_failure_never_substitutes_current_lake(retained: tuple, cause: str) -> None:
    evidence, row, blobs = retained
    evidence, row = with_input(evidence, row, blobs, unreadable=cause == "unreadable")
    point = failures.events(evidence)[0]
    assert point.expected_input_sha256 is not None
    path = blobs._path(point.expected_input_sha256)
    if cause == "missing":
        path.unlink()
    elif cause == "corrupt":
        path.write_bytes(b"changed bytes")
    elif cause == "changed":
        row["snapshot"]["manifest"][point.retained_path] = "f" * 64
    inspected = failures.inspect_record(evidence, row, point.session_open_ms, point.at_ms, point.leg_id, blobs=blobs)
    assert inspected.code == {"missing": "retained_file_unavailable", "corrupt": "retained_file_corrupt", "unreadable": "retained_file_unreadable", "changed": "retained_input_changed"}[cause]
    assert not inspected.input_verified and inspected.contains_eligible_trade_bar is None


@pytest.mark.parametrize("absent", [False, True])
def test_absent_or_unrecorded_inventory_does_not_read_an_unreceipted_file(retained: tuple, absent: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence, row, blobs = retained
    point = failures.events(evidence)[0]
    snapshot = Snapshot(manifest={}, absent=(point.retained_path,) if absent else (), retained_bytes=0)
    last = replace(evidence.result.pins[-1], option_days=())
    evidence = replace(evidence, result=replace(evidence.result, pins=(*evidence.result.pins[:-1], last)),
        protocol=evidence.protocol.model_copy(update={"snapshot_id": snapshot.snapshot_id}))
    row = {**row, "snapshot": snapshot.model_dump(mode="json")}
    def no_read(_digest: str) -> bytes:
        raise AssertionError("No unreceipted input may be read")
    monkeypatch.setattr(blobs, "get", no_read)
    view = failures.inspect_record(evidence, row, point.session_open_ms, point.at_ms, point.leg_id, blobs=blobs)
    assert view.code == ("retained_input_recorded_absent" if absent else "retained_input_not_recorded")
    assert not view.input_verified


async def test_http_reads_remain_admission_fenced_bounded_and_have_no_write_side_effect(retained: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence, row, blobs = retained
    digest = reader.setup_sha256(evidence.setup)
    calls = []
    async def load(study: str, selected: str, scope: str) -> reader.RetainedOptionsEvidence:
        calls.append((study, selected, scope))
        if scope == "final":
            raise LookupError("Final evidence is not exposed for this exact candidate.")
        return evidence
    async def get(_fn: Any, _study: str) -> dict[str, Any]:
        return row
    def no_write(_payload: bytes) -> str:
        raise AssertionError("Inspection must never store input bytes")
    monkeypatch.setattr(reader, "load", load)
    monkeypatch.setattr(failures, "with_connection", get)
    monkeypatch.setattr(failures, "default_blob_store", lambda: blobs)
    monkeypatch.setattr(blobs, "put", no_write)
    app = FastAPI()
    app.include_router(router)
    base = f"/studies/{row['id']}/candidate-evidence/{digest}/failures"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        viewed = await client.get(base)
        point = viewed.json()["items"][0]
        inspected = await client.get(base + "/inspect", params={"session_open_ms": point["session_open_ms"], "at_ms": point["at_ms"], "leg_id": point["leg_id"]})
        invalid = await client.get(base + "?limit=51")
        final = await client.get(base + "?window=final")
        undeclared = await client.get(base + "/inspect", params={"session_open_ms": point["session_open_ms"], "at_ms": point["at_ms"] + 1, "leg_id": point["leg_id"]})
    assert viewed.status_code == inspected.status_code == 200
    assert inspected.json()["code"] == "retained_file_unavailable"
    assert invalid.status_code == 422 and final.status_code == 404 and undeclared.status_code == 404
    assert len(calls) == 4


async def test_http_integrity_failure_is_unavailable_evidence_in_both_reads(retained: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence, row, blobs = retained
    data = json.loads((FIXTURES / "development-evidence.json").read_text())
    record = data["record"]
    record["result"]["metrics_sha256"] = "f" * 64
    async def read(fn: Any, *_args: Any) -> dict[str, Any]:
        return row if fn is repo.get else record
    monkeypatch.setattr(reader, "with_connection", read)
    monkeypatch.setattr(stages, "default_blob_store", lambda: blobs)
    app = FastAPI()
    app.include_router(router)
    point = failures.events(evidence)[0]
    base = f"/studies/{row['id']}/candidate-evidence/{reader.setup_sha256(evidence.setup)}/failures"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = [await client.get(base), await client.get(base + "/inspect", params={"session_open_ms": point.session_open_ms, "at_ms": point.at_ms, "leg_id": point.leg_id})]
    assert all(response.status_code == 503 for response in responses)
    assert all(response.json()["detail"]["code"] == "RETAINED_EVIDENCE_UNREADABLE" for response in responses)
