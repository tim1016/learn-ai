from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from app.engine.live.account_artifacts import AccountArtifactError, AccountFreezeEvidence
from app.services.account_reconciliation import AccountReconciliationService
from app.services.bot_lifecycle_conditions import lifecycle_conditions_for_instance


def _freeze(*, freeze_kind: Literal["account", "exposure"] = "account") -> AccountFreezeEvidence:
    return AccountFreezeEvidence(
        account_id="DU1234567",
        freeze_kind=freeze_kind,
        reason="watchdog.flatten_failed",
        source="watchdog_halt_executor",
        recorded_at_ms=1_700_000_000_000,
        operator_next_step="CHECK_IBKR",
    )


def test_lifecycle_conditions_returns_renderable_freeze_when_account_missing(tmp_path: Path) -> None:
    conditions = lifecycle_conditions_for_instance(
        tmp_path / "live-runs",
        account_id=None,
        sid="spy_ema_paper",
        account_freeze=_freeze(),
        now_ms=1_700_000_001_000,
    )

    assert len(conditions) == 1
    assert conditions[0].title == "Account freeze active"
    assert conditions[0].cure_action == "reconcile_now"
    assert conditions[0].cure_label == "Run account reconcile"


def test_lifecycle_conditions_returns_renderable_freeze_when_triage_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_triage(
        _self: AccountReconciliationService,
        **_kwargs: object,
    ) -> None:
        raise AccountArtifactError("triage unavailable")

    monkeypatch.setattr(AccountReconciliationService, "triage", fail_triage)

    conditions = lifecycle_conditions_for_instance(
        tmp_path / "live-runs",
        account_id="DU1234567",
        sid="spy_ema_paper",
        account_freeze=_freeze(freeze_kind="exposure"),
        now_ms=1_700_000_001_000,
    )

    assert len(conditions) == 1
    assert conditions[0].title == "Account freeze active"
    assert conditions[0].cure_action == "resolve_exposure"
    assert conditions[0].cure_label == "Resolve exposure"
