"""Golden and consumer-parity tests for the canonical Clerk fill fold (#1039)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkPositionEvidence,
)
from app.engine.live.journal_exposure import (
    ExecutionExposureEffect,
    fold_execution_exposure,
    project_journal_account_exposure,
    project_journal_exposure,
)

_FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "golden" / "journal-exposure-projection" / "journal.json"
)


def _load_fixture() -> dict[str, object]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_entries() -> list[AccountClerkJournalEntry]:
    fixture = _load_fixture()
    return [AccountClerkJournalEntry.model_validate(row) for row in fixture["entries"]]


def _projected_rows(entries: list[AccountClerkJournalEntry], *, group_by: str) -> list[dict[str, object]]:
    return [asdict(exposure) for exposure in project_journal_exposure(entries, group_by=group_by)]


def test_project_journal_exposure_matches_golden_fixture() -> None:
    fixture = _load_fixture()
    entries = _fixture_entries()

    assert _projected_rows(entries, group_by="namespace") == fixture["expected"]["namespace"]
    assert _projected_rows(entries, group_by="strategy_instance") == fixture["expected"]["strategy_instance"]


def test_fold_execution_exposure_normalizes_and_deduplicates() -> None:
    effects = [
        ExecutionExposureEffect("DUA", "ns-a", "spy", "exec-1", 2.0),
        ExecutionExposureEffect("DUA", "ns-a", "SPY", "exec-1", 99.0),
        ExecutionExposureEffect("DUA", "ns-a", "SPY", "exec-2", -0.5),
        ExecutionExposureEffect("DUB", "ns-a", "spy", "exec-1", 3.0),
        ExecutionExposureEffect("DUA", "ns-a", "QQQ", "exec-bad", float("nan")),
        ExecutionExposureEffect("DUA", "ns-flat", "IWM", "exec-3", 1.0),
        ExecutionExposureEffect("DUA", "ns-flat", "IWM", "exec-4", -1.0),
    ]

    assert fold_execution_exposure(effects) == {
        ("DUA", "ns-a", "SPY"): 1.5,
        ("DUB", "ns-a", "SPY"): 3.0,
    }


def test_fold_execution_exposure_prunes_sub_epsilon_residue() -> None:
    effects = [
        ExecutionExposureEffect("DUA", "ns-a", "SPY", "exec-1", 0.5e-9),
        ExecutionExposureEffect("DUA", "ns-a", "SPY", "exec-2", 0.25e-9),
    ]

    assert fold_execution_exposure(effects) == {}


def test_project_journal_exposure_redelivery_does_not_change_exposure() -> None:
    entries = _fixture_entries()
    without_redelivery = [
        entry
        for entry in entries
        if not (entry.intent.account_id == "DUA" and entry.seq == 3)
    ]

    assert project_journal_exposure(entries, group_by="namespace") == project_journal_exposure(
        without_redelivery,
        group_by="namespace",
    )


def test_project_journal_exposure_prunes_sub_epsilon_operator_adjustment() -> None:
    entry = AccountClerkJournalEntry(
        seq=1,
        entry_kind="operator_adjustment",
        recorded_at_ms=1_780_000_000_000,
        operator_adjustment=AccountClerkOperatorAdjustment(
            account_id="DUA",
            bot_order_namespace="learn-ai/ns-a/v1",
            symbol="SPY",
            signed_quantity=0.75e-9,
            request_provenance="test",
            reason="exercise the canonical flatness boundary",
            evidence_refs=("test:sub-epsilon-residue",),
            idempotency_key="sub-epsilon-residue",
            recorded_at_ms=1_780_000_000_000,
        ),
    )

    assert project_journal_exposure([entry], group_by="namespace") == ()


def test_project_journal_exposure_does_not_deduplicate_matching_exec_ids_across_accounts() -> None:
    exposures = project_journal_exposure(_fixture_entries(), group_by="namespace")

    assert [(exposure.account_id, exposure.symbol, exposure.quantity) for exposure in exposures] == [
        ("DUA", "SPY", 3.0),
        ("DUB", "SPY", 2.0),
    ]


def test_account_projection_includes_unattributed_callbacks() -> None:
    entries = _fixture_entries()
    source = next(entry for entry in entries if entry.entry_kind == "broker_event")
    assert source.broker_event is not None
    foreign_event = {
        **source.broker_event,
        "account_id": "DUA",
        "order_ref": "manual-tws-order",
        "exec_id": "foreign-exec-1044",
        "fill_quantity": 4.0,
        "side": "BUY",
    }
    unattributed = AccountClerkJournalEntry(
        seq=999,
        entry_kind="broker_event",
        recorded_at_ms=999,
        broker_event=foreign_event,
        event_account_id="DUA",
        broker_callback_idempotency_key="fill|foreign-exec-1044|manual",
    )

    namespace_before = project_journal_exposure(entries, group_by="namespace")
    namespace_after = project_journal_exposure([*entries, unattributed], group_by="namespace")
    account_exposure = project_journal_account_exposure([*entries, unattributed], account_id="DUA")

    assert namespace_after == namespace_before
    assert ("DUA", "SPY", 7.0) in {
        (exposure.account_id, exposure.symbol, exposure.quantity) for exposure in account_exposure
    }


def test_broker_evidence_baseline_is_account_visible_but_never_given_a_bot_namespace() -> None:
    baseline = AccountClerkJournalEntry(
        seq=1,
        entry_kind="broker_evidence_baseline",
        recorded_at_ms=1_780_000_000_000,
        broker_evidence_baseline=AccountClerkBrokerEvidenceBaseline(
            account_id="DUA",
            observed_at_ms=1_780_000_000_000,
            positions=(
                AccountClerkPositionEvidence(
                    symbol="SPY",
                    signed_quantity=2.0,
                    evidence_observed_at_ms=1_780_000_000_000,
                ),
            ),
        ),
    )

    account_rows = project_journal_account_exposure([baseline], account_id="DUA")

    assert [(row.account_id, row.symbol, row.quantity) for row in account_rows] == [("DUA", "SPY", 2.0)]
    assert project_journal_exposure([baseline], group_by="namespace") == ()


def test_project_journal_account_exposure_prunes_sub_epsilon_baseline() -> None:
    baseline = AccountClerkJournalEntry(
        seq=1,
        entry_kind="broker_evidence_baseline",
        recorded_at_ms=1_780_000_000_000,
        broker_evidence_baseline=AccountClerkBrokerEvidenceBaseline(
            account_id="DUA",
            observed_at_ms=1_780_000_000_000,
            positions=(
                AccountClerkPositionEvidence(
                    symbol="SPY",
                    signed_quantity=0.75e-9,
                    evidence_observed_at_ms=1_780_000_000_000,
                ),
            ),
        ),
    )

    assert project_journal_account_exposure([baseline], account_id="DUA") == ()
