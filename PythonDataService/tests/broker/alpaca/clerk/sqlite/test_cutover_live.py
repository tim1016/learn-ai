"""A never-legacy account graduates through the cutover ceremony (ADR 0059 D1/D11, slice 7 R3, owner decision 2026-09-14).

The ceremony is offline and its evidence file is hand-authored; an empty
legacy set is permitted for either mode (R3's live-only exception was
removed — the asymmetry between a never-legacy live account and a
never-legacy paper account served no safety purpose the flat-and-order-free
check below did not already cover). Shadow rehearsal is a mode, not a
requirement (owner decision 2026-09-09); flat and order-free is what still
guards graduation, for both modes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.cutover import (
    BrokerCutoverEvidence,
    CutoverInitializationReceipt,
    CutoverRefused,
    apply_cutover,
    initialize_cutover_authority,
    plan_cutover,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

NOW = 1_800_000_000_000
MAX_AGE_MS = 600_000


def _evidence(
    *,
    account_id: str = LIVE_ACCT,
    account_mode: str = "live",
    positions: dict[str, float] | None = None,
    open_order_ids: tuple[str, ...] = (),
) -> BrokerCutoverEvidence:
    return BrokerCutoverEvidence(
        account_id=account_id,
        account_mode=account_mode,
        observed_at_ms=NOW - 1_000,
        proof_reference="alpaca-account-read:2026-09-09",
        positions=positions or {},
        open_order_ids=tuple(open_order_ids),
    )


def _initialize(tmp_path: Path, evidence: BrokerCutoverEvidence) -> CutoverInitializationReceipt:
    (tmp_path / "runner" / "live_state").mkdir(parents=True, exist_ok=True)
    return initialize_cutover_authority(
        account_id=evidence.account_id,
        artifacts_root=tmp_path,
        runner_artifacts_root=tmp_path / "runner",
        broker_evidence=evidence,
        max_broker_evidence_age_ms=MAX_AGE_MS,
        clock=lambda: NOW,
    )


_NEVER_LEGACY_ACCOUNTS = (
    pytest.param(LIVE_ACCT, "live", id="live"),
    pytest.param("PANEVER01", "paper", id="paper"),
)


@pytest.mark.parametrize(("account_id", "account_mode"), _NEVER_LEGACY_ACCOUNTS)
def test_a_never_legacy_never_rehearsed_account_initializes(
    tmp_path: Path, account_id: str, account_mode: str
) -> None:
    """Shadow is a mode, not a requirement (owner decision 2026-09-09).

    Neither mode requires legacy evidence to initialize (owner decision
    2026-09-14 removed R3's live-only exception).
    """
    receipt = _initialize(tmp_path, _evidence(account_id=account_id, account_mode=account_mode))
    assert receipt.account_id == account_id
    assert receipt.broker_evidence.account_mode == account_mode
    assert receipt.legacy_artifacts == ()
    assert receipt.runner_roster == ()
    ClerkSqliteRepository.open(
        account_id=account_id, artifacts_root=tmp_path, clock=lambda: NOW
    ).close()


@pytest.mark.parametrize(("account_id", "account_mode"), _NEVER_LEGACY_ACCOUNTS)
def test_a_never_legacy_account_graduates_end_to_end(
    tmp_path: Path, account_id: str, account_mode: str
) -> None:
    """R3: initialize → plan → apply with an empty legacy set, on either mode's evidence alone."""
    evidence = _evidence(account_id=account_id, account_mode=account_mode)
    _initialize(tmp_path, evidence)

    plan = plan_cutover(
        account_id=account_id,
        artifacts_root=tmp_path,
        runner_artifacts_root=tmp_path / "runner",
        broker_evidence=evidence,
        max_broker_evidence_age_ms=MAX_AGE_MS,
        clock=lambda: NOW,
    )
    assert plan.legacy_artifacts == ()

    receipt = apply_cutover(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        artifacts_root=tmp_path,
        runner_artifacts_root=tmp_path / "runner",
        broker_evidence=evidence,
        max_broker_evidence_age_ms=MAX_AGE_MS,
        clock=lambda: NOW,
    )

    store = ActivationStore(tmp_path / "accounts" / "alpaca")
    assert store.latest(account_id) == receipt.activation
    assert (tmp_path / receipt.receipt_reference).is_file()


@pytest.mark.parametrize(("account_id", "account_mode"), _NEVER_LEGACY_ACCOUNTS)
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"positions": {"SPY": 3.0}}, "broker-flat"),
        ({"open_order_ids": ("o-1",)}, "no open broker orders"),
    ],
)
def test_an_account_must_be_flat_and_order_free_to_graduate(
    tmp_path: Path,
    account_id: str,
    account_mode: str,
    kwargs: dict[str, object],
    match: str,
) -> None:
    """R3 / owner question E2: the shadow authority never submitted, so any position is a human's.

    Flat-and-order-free guards both modes identically — it is what makes the
    empty-legacy exception safe to extend to paper (owner decision 2026-09-14).
    """
    with pytest.raises(CutoverRefused, match=match):
        _initialize(tmp_path, _evidence(account_id=account_id, account_mode=account_mode, **kwargs))


def test_an_unknown_mode_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(CutoverRefused, match="paper or live"):
        _initialize(tmp_path, _evidence(account_mode="sandbox"))
