"""Service helpers for account/fleet contamination projections."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.engine.live.account_clerk import read_account_clerk_journal
from app.engine.live.fleet import compute_fleet_contamination
from app.engine.live.journal_exposure import project_journal_exposure
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.engine.live.run_ledger import read_ledger
from app.schemas.live_runs import FleetContamination, InstanceBrokerView
from app.services.account_journal_authority import (
    account_journal_authority_is_active,
    observe_account_journal_parity,
)
from app.services.legacy_stale_claim_retirement import retired_legacy_claim_keys

logger = logging.getLogger(__name__)
NetPositionFetcher = Callable[[], Awaitable[dict[str, int] | None]]


class AccountJournalScopeRequiredError(ValueError):
    """Raised rather than allowing two account journals to net in one verdict."""


class BrokerAccountMismatchError(ValueError):
    """The connected broker proved it is serving a different account."""


def scan_runs_by_instance(root: Path) -> dict[str, list[dict]]:
    """Group run dirs by ``strategy_instance_id`` from their ledgers, newest first."""

    out: dict[str, list[dict]] = {}
    if not root.is_dir():
        return out
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            ledger = json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = ledger.get("strategy_instance_id") or ""
        if not sid:
            continue
        out.setdefault(sid, []).append(
            {
                "run_id": str(ledger.get("run_id") or run_dir.name),
                "run_dir": str(run_dir),
                "created_at_ms": ledger.get("created_at_ms") or 0,
            }
        )
    for runs in out.values():
        runs.sort(key=lambda r: r["created_at_ms"], reverse=True)
    return out


def read_instance_live_state(root: Path, sid: str) -> LiveStateEnvelope | None:
    artifacts_root = root.parent
    try:
        sidecar_path = stable_live_state_path(artifacts_root, sid)
    except ValueError:
        return None
    try:
        return LiveStateSidecarRepo(
            sidecar_path, trusted_root=artifacts_root / "live_state"
        ).read()
    except (LiveStateSidecarCorruptError, OSError):
        return None


def instance_broker(root: Path, sid: str) -> InstanceBrokerView | None:
    """Read an instance's namespace-attributed broker slice from live state."""

    envelope = read_instance_live_state(root, sid)
    if envelope is None:
        return None
    return InstanceBrokerView(
        bot_order_namespace=envelope.bot_order_namespace,
        owned_positions=dict(envelope.expected_position_by_symbol),
        pending_order_count=len(envelope.pending_intents),
    )


def collect_fleet_position_explanations(
    root: Path,
    *,
    account_id: str | None = None,
) -> dict[str, dict[str, int]]:
    """Read canonical Clerk-journal exposure; sidecars are bot-local only.

    Formula: residual[symbol] = broker_net[symbol] - Σ journal_namespace_exposure[symbol]
    Reference: ADR 0030 account-rooted journal; issue #1024.
    Canonical implementation: this function and ``compute_fleet_contamination``.
    Validated against: tests/services/test_fleet_contamination.py::test_journal_exposure_is_canonical.
    """

    journal_explained = (
        _collect_journal_position_explanations(root)
        if account_id is None
        else _collect_journal_position_explanations(root, account_id=account_id)
    )
    if journal_explained is not None:
        # Contamination/status reads are pure projections.  Parity evidence
        # belongs to a bounded background/state-change observer, never here.
        return journal_explained

    # No account journal has ever been created. Retain the legacy read only
    # during the shadow bootstrap; once an account has a Clerk journal its
    # stale per-run sidecars can never contribute to account truth again.
    return (
        _collect_legacy_fleet_position_explanations(root)
        if account_id is None
        else _collect_legacy_fleet_position_explanations(root, account_id=account_id)
    )


def _collect_journal_position_explanations(
    root: Path,
    *,
    account_id: str | None = None,
) -> dict[str, dict[str, int]] | None:
    artifacts_root = root.parent
    accounts_root = artifacts_root / "accounts"
    if not accounts_root.is_dir():
        return None
    explained: dict[str, dict[str, int]] = {}
    found_journal = False
    for account_dir in sorted(path for path in accounts_root.iterdir() if path.is_dir()):
        if account_id is not None and account_dir.name != account_id:
            continue
        journal_path = account_dir / "clerk_journal.jsonl"
        if not journal_path.exists():
            continue
        if account_id is None and found_journal:
            raise AccountJournalScopeRequiredError("ACCOUNT_JOURNAL_SCOPE_REQUIRED")
        found_journal = True
        entries = read_account_clerk_journal(artifacts_root, account_dir.name)
        for exposure in project_journal_exposure(
            entries,
            account_id=account_dir.name,
            group_by="strategy_instance",
        ):
            positions = explained.setdefault(exposure.group_id, {})
            positions[exposure.symbol] = int(exposure.quantity)
    return explained if found_journal else None


def _record_sidecar_journal_parity(
    root: Path,
    journal: dict[str, dict[str, int]],
    legacy: dict[str, dict[str, int]],
    *,
    account_id: str | None,
) -> bool:
    """Record one account-scoped shadow observation and evaluate authority."""

    now_ms = time.time_ns() // 1_000_000
    accounts_root = root.parent / "accounts"
    for account_dir in accounts_root.iterdir() if accounts_root.is_dir() else ():
        if account_id is not None and account_dir.name != account_id:
            continue
        if (account_dir / "clerk_journal.jsonl").exists():
            observe_account_journal_parity(
                root.parent,
                account_dir.name,
                journal=journal,
                legacy=legacy,
                now_ms=now_ms,
            )
    return all(
        account_journal_authority_is_active(root.parent, path.name)
        for path in accounts_root.iterdir()
        if path.is_dir()
        and (account_id is None or path.name == account_id)
        and (path / "clerk_journal.jsonl").exists()
    )


def record_account_journal_parity_observation(
    root: Path,
    *,
    account_id: str,
) -> bool:
    """Observe one account outside read APIs, with durable cadence fencing."""

    journal = _collect_journal_position_explanations(root, account_id=account_id)
    if journal is None:
        return False
    legacy = _collect_legacy_fleet_position_explanations(root, account_id=account_id)
    return _record_sidecar_journal_parity(
        root,
        journal,
        legacy,
        account_id=account_id,
    )


def _collect_legacy_fleet_position_explanations(
    root: Path,
    *,
    account_id: str | None = None,
) -> dict[str, dict[str, int]]:
    """Deprecated shadow comparator; never feeds the account verdict."""

    explained: dict[str, dict[str, int]] = {}
    retired_by_account: dict[str, frozenset[tuple[str, str, str, str]]] = {}
    for sid in scan_runs_by_instance(root):
        envelope = read_instance_live_state(root, sid)
        if envelope is not None and envelope.expected_position_by_symbol:
            if account_id is not None:
                try:
                    ledger = read_ledger(root / envelope.run_id / "run_ledger.json")
                except (OSError, ValueError):
                    continue
                if ledger.account_id != account_id:
                    continue
            retired = _retired_claim_keys_for_run(
                artifacts_root=root.parent, run_id=envelope.run_id, cache=retired_by_account
            )
            positions = {
                symbol: quantity
                for symbol, quantity in envelope.expected_position_by_symbol.items()
                if (sid, envelope.run_id, symbol.upper(), envelope.bot_order_namespace) not in retired
            }
            if positions:
                explained[sid] = positions
    return explained


def _retired_claim_keys_for_run(
    *,
    artifacts_root: Path,
    run_id: str,
    cache: dict[str, frozenset[tuple[str, str, str, str]]],
) -> frozenset[tuple[str, str, str, str]]:
    """Fold retirement receipts once per account, keeping legacy sidecars read-only.

    Failure keeps claims visible (fail-safe): an unreadable ledger or event log
    must never hide managed exposure from the contamination sum.
    """

    try:
        ledger = read_ledger(artifacts_root / "live_runs" / run_id / "run_ledger.json")
    except (OSError, ValueError):
        logger.debug("legacy retirement filter: no readable ledger for run %s; claims stay visible", run_id)
        return frozenset()
    account_id = ledger.account_id
    if not account_id:
        return frozenset()
    if account_id not in cache:
        try:
            cache[account_id] = retired_legacy_claim_keys(artifacts_root, account_id)
        except (OSError, ValueError) as exc:
            logger.warning(
                "legacy retirement filter: receipts unreadable for %s (%s); claims stay visible",
                account_id,
                exc,
            )
            cache[account_id] = frozenset()
    return cache[account_id]


async def fetch_net_positions(account_id: str | None = None) -> dict[str, int] | None:
    """Best-effort net position only when the broker proves the account id."""

    try:
        from app.broker.ibkr import account as ibkr_account
        from app.routers.broker_dependencies import require_connected_client

        client = require_connected_client()
        snapshot = await ibkr_account.fetch_positions(client)
    except Exception as exc:
        logger.info("fleet net-position fetch unavailable: %s", exc)
        return None
    if account_id is not None and snapshot.account_id.upper() != account_id.upper():
        logger.warning(
            "fleet net-position account mismatch",
            extra={"requested_account_id": account_id, "broker_account_id": snapshot.account_id},
        )
        raise BrokerAccountMismatchError(
            f"BROKER_ACCOUNT_MISMATCH:{account_id}:{snapshot.account_id}"
        )
    net: dict[str, int] = {}
    for pos in snapshot.positions:
        symbol = str(pos.symbol).upper()
        net[symbol] = net.get(symbol, 0) + int(pos.quantity)
    return net


async def compute_account_fleet_contamination(
    root: Path,
    fetch_positions: NetPositionFetcher | None = None,
    *,
    account_id: str | None = None,
) -> FleetContamination:
    resolve_net_positions = fetch_positions or (lambda: fetch_net_positions(account_id))
    explanations = collect_fleet_position_explanations(root, account_id=account_id)
    try:
        net_positions = await resolve_net_positions()
    except BrokerAccountMismatchError:
        result = compute_fleet_contamination(None, explanations, policy_blocks_starts=True)
        result["policy_blocks_starts"] = True
        result["summary"] = "Connected broker account mismatches the requested account; starts are blocked."
    else:
        result = compute_fleet_contamination(
            net_positions,
            explanations,
            policy_blocks_starts=True,
        )
    return FleetContamination(**result)
