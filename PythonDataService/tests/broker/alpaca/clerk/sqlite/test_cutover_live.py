"""A live account graduates through the paper cutover ceremony, widened (ADR 0059 D1/D11, slice 7 R3).

The ceremony is offline and its evidence file is hand-authored, so the only
mode fact it can prove is the evidence's own word; for ``live`` that word
permits the empty legacy set a never-legacy live account cannot fill. Shadow
rehearsal is a mode, not a requirement (owner decision 2026-09-09); flat and
order-free is what still guards graduation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverRefused,
    initialize_cutover_authority,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

NOW = 1_800_000_000_000
MAX_AGE_MS = 600_000
# The runner-roster reader validates account_id against
# app.engine.live.account_artifacts._ACCOUNT_ID_RE (``^[A-Z][A-Z0-9]+$``,
# letter-led, unrelated to and unchanged by this task); LIVE_ACCT
# ("9LIVE0001") is digit-led and only reaches that check in the one test
# below that has no legacy strategy instance ids to fall back on.
_RUNNER_SAFE_LIVE_ACCT = "LIVE0001"


def _evidence(*, account_id: str = LIVE_ACCT, account_mode: str = "live", positions=None, open_order_ids=()) -> BrokerCutoverEvidence:
    return BrokerCutoverEvidence(
        account_id=account_id,
        account_mode=account_mode,
        observed_at_ms=NOW - 1_000,
        proof_reference="alpaca-account-read:2026-09-09",
        positions=positions or {},
        open_order_ids=tuple(open_order_ids),
    )


def _initialize(tmp_path: Path, evidence: BrokerCutoverEvidence):
    (tmp_path / "runner" / "live_state").mkdir(parents=True, exist_ok=True)
    return initialize_cutover_authority(
        account_id=evidence.account_id,
        artifacts_root=tmp_path,
        runner_artifacts_root=tmp_path / "runner",
        broker_evidence=evidence,
        max_broker_evidence_age_ms=MAX_AGE_MS,
        clock=lambda: NOW,
    )


def test_a_never_legacy_never_rehearsed_live_account_initializes(tmp_path: Path) -> None:
    """Shadow is a mode, not a requirement (owner decision 2026-09-09)."""
    receipt = _initialize(tmp_path, _evidence(account_id=_RUNNER_SAFE_LIVE_ACCT))
    assert receipt.account_id == _RUNNER_SAFE_LIVE_ACCT
    assert receipt.broker_evidence.account_mode == "live"
    assert receipt.legacy_artifacts == ()
    assert receipt.runner_roster == ()
    ClerkSqliteRepository.open(
        account_id=_RUNNER_SAFE_LIVE_ACCT, artifacts_root=tmp_path, clock=lambda: NOW
    ).close()


def test_a_never_legacy_paper_account_still_refuses(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="no legacy authority artifacts"):
        _initialize(tmp_path, _evidence(account_id="PA-NEVER", account_mode="paper"))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"positions": {"SPY": 3.0}}, "broker-flat"),
        ({"open_order_ids": ("o-1",)}, "no open broker orders"),
    ],
)
def test_a_live_account_must_be_flat_and_order_free_to_graduate(tmp_path: Path, kwargs, match: str) -> None:
    """R3 / owner question E2: the shadow authority never submitted, so any position is a human's."""
    with pytest.raises(CutoverRefused, match=match):
        _initialize(tmp_path, _evidence(**kwargs))


def test_an_unknown_mode_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="paper or live"):
        _initialize(tmp_path, _evidence(account_mode="sandbox"))
