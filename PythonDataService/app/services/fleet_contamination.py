"""Service helpers for account/fleet contamination projections."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.engine.live.account_clerk_journal import read_account_clerk_journal
from app.engine.live.account_identity import InvalidAccountIdError, normalize_account_id
from app.engine.live.journal_exposure import project_journal_exposure
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.engine.live.run_lookup import account_id_from_run_ledger
from app.schemas.live_runs import InstanceBrokerView
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
        envelope = LiveStateSidecarRepo(
            sidecar_path, trusted_root=artifacts_root / "live_state"
        ).read()
        if envelope is None or envelope.strategy_instance_id != sid:
            return None
        return envelope
    except (LiveStateSidecarCorruptError, OSError):
        return None


def instance_broker(root: Path, sid: str) -> InstanceBrokerView | None:
    """Read an instance's Clerk-owned exposure plus local pending intents.

    Formula: instance_position[symbol] =
      ClerkJournalExposure[account, strategy_instance, symbol].
    Reference: ADR 0030; docs/architecture/engine-authority-map.md,
      "Live account contamination verdict".
    Canonical implementation: journal_exposure.py::project_journal_exposure;
      this function is a fleet-safety consumer.
    Validated against:
      tests/services/test_fleet_contamination.py::
      test_instance_broker_uses_clerk_positions_not_stale_sidecar.

    ``expected_position_by_symbol`` remains a compatibility fallback only when
    the run ledger identifies an account and no account journal exists. Once
    the Clerk journal exists, a retired or crashed run's stale sidecar must not
    drive current risk or replacement deployment decisions. A missing or
    corrupt ledger cannot name the account journal to consult, so it is
    unknown evidence rather than permission to reuse sidecar exposure.
    """

    envelope = read_instance_live_state(root, sid)
    if envelope is None:
        return None
    account_id = _account_id_for_run(root, envelope.run_id)
    if account_id is None:
        return None
    journal_positions = _collect_journal_position_explanations(
        root,
        account_id=account_id,
    )
    owned_positions = (
        journal_positions.get(sid, {})
        if journal_positions is not None
        else dict(envelope.expected_position_by_symbol)
    )
    return InstanceBrokerView(
        bot_order_namespace=envelope.bot_order_namespace,
        owned_positions=owned_positions,
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
                ledger_account_id = _account_id_for_run(root, envelope.run_id)
                if ledger_account_id != account_id:
                    continue
            retired = _retired_claim_keys_for_run(
                artifacts_root=root.parent, run_id=envelope.run_id, cache=retired_by_account
            )
            positions = {
                symbol: quantity
                for symbol, quantity in envelope.expected_position_by_symbol.items()
                if quantity != 0
                and (sid, envelope.run_id, symbol.upper(), envelope.bot_order_namespace) not in retired
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

    account_id = _account_id_for_run(artifacts_root / "live_runs", run_id)
    if account_id is None:
        logger.debug("legacy retirement filter: no readable ledger for run %s; claims stay visible", run_id)
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


def _account_id_for_run(live_runs_root: Path, run_id: str) -> str | None:
    """Read a legacy ledger only after proving its run-directory identity."""

    if not run_id or Path(run_id).name != run_id:
        return None
    try:
        root = live_runs_root.resolve()
        run_dir = (root / run_id).resolve()
    except OSError:
        return None
    if run_dir.parent != root:
        return None
    raw_account_id = account_id_from_run_ledger(run_dir, expected_run_id=run_id)
    if raw_account_id is None:
        return None
    try:
        return normalize_account_id(raw_account_id)
    except InvalidAccountIdError:
        return None


