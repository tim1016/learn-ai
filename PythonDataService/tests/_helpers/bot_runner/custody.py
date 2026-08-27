"""Registry construction and custody-snapshot test support shared by the
bot_runner test package and outside suites that exercise ``BotTaskRegistry``
end to end (deploy/stop/crash-recovery flows reached via the router,
boot-recovery, and canary-rollback suites under ``tests/``).

Split out of ``tests/services/bot_runner/conftest.py`` per issue #1810 --
see that module's sibling ``doubles.py``/``market.py``/``ema_parity.py``
for the other extracted themes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
    InstanceCustodyProof,
)
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.services.bot_runner import BotTaskRegistry
from app.utils.timestamps import now_ms_utc

from .market import _tradable_market_liveness

if TYPE_CHECKING:
    # Type-hint only: importing ``doubles`` for real here would cycle back,
    # since ``doubles._SqliteRuntimeBroker`` imports ``_T0`` from this module.
    from .doubles import _FakeFeed

_SID = "alpaca-skeleton-1"
_T0 = 1_700_000_000_000


def _custody_proof(
    *,
    exposure: dict[str, float],
    verdict: str = "clean",
    freeze: AccountFreezeState | None = None,
) -> InstanceCustodyProof:
    return InstanceCustodyProof(
        account_id="paper-account",
        strategy_instance_id=_SID,
        reconciliation_verdict=verdict,  # type: ignore[arg-type]
        freeze=freeze or AccountFreezeState(),
        exposure=exposure,
        observed_at_ms=_T0,
    )


def _flat_custody_snapshot(
    sid: str,
    *,
    observed_at_ms: int | None = None,
) -> ClerkCustodySnapshot:
    observed_at_ms = observed_at_ms or now_ms_utc()
    zero = CustodyCountFact(state="zero", count=0)
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id="paper-account",
        strategy_instance_id=sid,
        clerk_generation="test-clerk",
        journal_sequence=0,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=zero,
        pending_orders=zero,
        terminal_orders=zero,
        unresolved_effects=zero,
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=("test-clerk:0",),
        observed_at_ms=observed_at_ms,
    )


@asynccontextmanager
async def _flat_start_guard(sid: str):
    yield _flat_custody_snapshot(sid)


def _registry(
    tmp_path: Path,
    feed: _FakeFeed | None,
    *,
    policy: RestartIntensityPolicy | None = None,
    now_ms: Callable[[], int] = now_ms_utc,
    start_custody_guard: (
        Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]] | None
    ) = None,
) -> BotTaskRegistry:
    return BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy or RestartIntensityPolicy(threshold=100),
        # Boot recovery has its own suite (test_boot_recovery.py).
        boot_recovery_required=False,
        start_custody_guard=start_custody_guard or _flat_start_guard,
        now_ms=now_ms,
        market_liveness=_tradable_market_liveness,
    )


def _lifecycle_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "lifecycle_state.json"
    return json.loads(path.read_text(encoding="utf-8"))
