"""Golden and consumer-parity tests for the canonical Clerk fill fold (#1039)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import pytest

import app.services.fleet_contamination as fleet_contamination
from app.engine.live.account_clerk import AccountClerkJournalEntry
from app.engine.live.account_clerk_reconciler import namespace_expected_exposure
from app.engine.live.journal_exposure import project_journal_exposure
from app.services.fleet_contamination import (
    AccountJournalScopeRequiredError,
    collect_fleet_position_explanations,
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


def _write_journals(root: Path, entries: list[AccountClerkJournalEntry]) -> None:
    per_account: dict[str, list[AccountClerkJournalEntry]] = defaultdict(list)
    for entry in entries:
        per_account[entry.intent.account_id].append(entry)
    for account_id, account_entries in per_account.items():
        journal_path = root / "accounts" / account_id / "clerk_journal.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(
            "".join(f"{entry.model_dump_json()}\n" for entry in account_entries),
            encoding="utf-8",
        )


def test_project_journal_exposure_matches_golden_fixture() -> None:
    fixture = _load_fixture()
    entries = _fixture_entries()

    assert _projected_rows(entries, group_by="namespace") == fixture["expected"]["namespace"]
    assert _projected_rows(entries, group_by="strategy_instance") == fixture["expected"]["strategy_instance"]


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


def test_project_journal_exposure_does_not_deduplicate_matching_exec_ids_across_accounts() -> None:
    exposures = project_journal_exposure(_fixture_entries(), group_by="namespace")

    assert [(exposure.account_id, exposure.symbol, exposure.quantity) for exposure in exposures] == [
        ("DUA", "SPY", 3.0),
        ("DUB", "SPY", 2.0),
    ]


def test_reconciler_and_contamination_share_the_journal_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _fixture_entries()
    _write_journals(tmp_path, entries)
    root = tmp_path / "live_runs"
    root.mkdir()

    expected = {"bot-alpha": {"SPY": 3}}
    monkeypatch.setattr(
        fleet_contamination,
        "_collect_legacy_fleet_position_explanations",
        lambda _root, **kwargs: expected
        if kwargs.get("account_id") == "DUA"
        else {"bot-alpha": {"SPY": 2}},
    )
    for _ in range(3):
        contamination_view = collect_fleet_position_explanations(root, account_id="DUA")

    reconciler_view = namespace_expected_exposure(
        [entry for entry in entries if entry.intent.account_id == "DUA"]
    )
    reconciler_by_symbol: dict[str, float] = defaultdict(float)
    for exposure in reconciler_view:
        reconciler_by_symbol[exposure.symbol] += exposure.quantity

    assert contamination_view == expected
    assert dict(reconciler_by_symbol) == {"SPY": 3.0}
    assert collect_fleet_position_explanations(root, account_id="DUB") == {"bot-alpha": {"SPY": 2}}
    with pytest.raises(AccountJournalScopeRequiredError, match="ACCOUNT_JOURNAL_SCOPE_REQUIRED"):
        collect_fleet_position_explanations(root)
