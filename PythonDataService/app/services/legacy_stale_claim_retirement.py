"""Proof-required retirement of legacy per-run sidecar exposure claims.

This is deliberately an archaeology adapter.  It records append-only account
event receipts and leaves the historical sidecars untouched; the legacy fleet
projection folds those receipts until the Clerk journal becomes canonical.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    append_account_event,
    read_account_events,
)
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_registry import read_account_instance_registry
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.engine.live.run_ledger import LiveRunLedger, read_ledger
from app.schemas.account_reconciliation import (
    LegacyStaleClaimCandidate,
    LegacyStaleClaimRetirementReceipt,
)
from app.schemas.account_truth import AccountTruthPositionRow, AccountTruthResponse
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.utils.timestamps import now_ms_utc

LEGACY_STALE_CLAIM_RETIRED_EVENT = "legacy_stale_claim_retired"
_SAFE_PROCESS_STATES = frozenset({"exited"})
_KNOWN_ELSEWHERE_OWNER_CLASSES = frozenset({"bot", "manual"})
RunProcessFetcher = Callable[[str], Awaitable[tuple[DaemonResult, dict | None]]]


class LegacyStaleClaimRetirementError(AccountArtifactError):
    """A proof required to retire a legacy claim is absent or contradictory."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class LegacyStaleClaim:
    """One non-zero sidecar claim belonging to a historical account run."""

    account_id: str
    strategy_instance_id: str
    run_id: str
    bot_order_namespace: str
    symbol: str
    claimed_quantity: int
    created_at_ms: int

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.strategy_instance_id, self.run_id, self.symbol, self.bot_order_namespace)

    @property
    def claim_id(self) -> str:
        payload = "\x1f".join((*self.key, str(self.claimed_quantity))).encode("utf-8")
        return f"legacy-claim-{hashlib.sha256(payload).hexdigest()[:24]}"


class LegacyStaleClaimRetirementService:
    """Discover, prove, and receipt one-time legacy sidecar retirements."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    async def candidates(
        self,
        *,
        account_id: str,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
    ) -> list[LegacyStaleClaimCandidate]:
        """Return only claims whose current proof chain is complete."""

        candidates: list[LegacyStaleClaimCandidate] = []
        for claim in self.claims_for_account(account_id):
            try:
                candidates.append(
                    await self._prove_claim(
                        claim=claim,
                        account_truth=account_truth,
                        fetch_run_process=fetch_run_process,
                    )
                )
            except LegacyStaleClaimRetirementError:
                continue
        return candidates

    async def retire(
        self,
        *,
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
        symbol: str,
        requested_by: str,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
    ) -> LegacyStaleClaimRetirementReceipt:
        """Re-prove and append one retirement receipt immediately before mutation."""

        canonical_account_id = normalize_account_id(account_id)
        claim = self._find_claim(
            account_id=canonical_account_id,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            symbol=symbol,
        )
        candidate = await self._prove_claim(
            claim=claim,
            account_truth=account_truth,
            fetch_run_process=fetch_run_process,
        )
        retired_at_ms = self._now_ms()
        receipt = LegacyStaleClaimRetirementReceipt(
            receipt_id=f"legacy-retirement-{candidate.claim_id}",
            account_id=claim.account_id,
            strategy_instance_id=claim.strategy_instance_id,
            run_id=claim.run_id,
            bot_order_namespace=claim.bot_order_namespace,
            symbol=claim.symbol,
            claimed_quantity=claim.claimed_quantity,
            requested_by=requested_by,
            retired_at_ms=retired_at_ms,
        )
        appended = await asyncio.to_thread(
            append_account_event,
            self._artifacts_root,
            claim.account_id,
            {
                "event_type": LEGACY_STALE_CLAIM_RETIRED_EVENT,
                "receipt_id": receipt.receipt_id,
                "strategy_instance_id": claim.strategy_instance_id,
                "run_id": claim.run_id,
                "bot_order_namespace": claim.bot_order_namespace,
                "symbol": claim.symbol,
                "claimed_quantity": claim.claimed_quantity,
                "requested_by": requested_by,
                "process_state": "dead",
                "binding_state": "RETIRED_OR_ABSENT",
                "broker_proof": candidate.proof_summary,
                "recorded_at_ms": retired_at_ms,
            },
            only_if_receipt_absent=True,
        )
        if not appended:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_ALREADY_RETIRED",
                "A retirement receipt already exists for this legacy claim.",
            )
        return receipt

    def claims_for_account(self, account_id: str) -> list[LegacyStaleClaim]:
        """Read eligible obsolete sidecar claims without altering their files.

        A sidecar can become stale before or after the Clerk journal begins.
        The fresh broker, dead-process, and retired-binding proof chain below
        owns retirement safety; journal age cannot leave a post-Clerk claim
        without a remedy.
        """

        canonical_account_id = normalize_account_id(account_id)
        retired = retired_legacy_claim_keys(self._artifacts_root, canonical_account_id)
        claims: list[LegacyStaleClaim] = []
        live_state_root = self._artifacts_root / "live_state"
        if not live_state_root.is_dir():
            return claims
        for sidecar_dir in sorted(path for path in live_state_root.iterdir() if path.is_dir()):
            try:
                envelope = LiveStateSidecarRepo(
                    stable_live_state_path(self._artifacts_root, sidecar_dir.name),
                    trusted_root=live_state_root,
                ).read()
            except (LiveStateSidecarCorruptError, OSError, ValueError):
                continue
            if envelope is None:
                continue
            ledger = self._read_claim_ledger(envelope.run_id)
            if ledger is None or normalize_account_id(ledger.account_id) != canonical_account_id:
                continue
            for raw_symbol, raw_quantity in envelope.expected_position_by_symbol.items():
                if raw_quantity == 0:
                    continue
                claim = LegacyStaleClaim(
                    account_id=canonical_account_id,
                    strategy_instance_id=envelope.strategy_instance_id,
                    run_id=envelope.run_id,
                    bot_order_namespace=envelope.bot_order_namespace,
                    symbol=raw_symbol.upper(),
                    claimed_quantity=int(raw_quantity),
                    created_at_ms=ledger.created_at_ms,
                )
                if claim.key not in retired:
                    claims.append(claim)
        return claims

    async def _prove_claim(
        self,
        *,
        claim: LegacyStaleClaim,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
    ) -> LegacyStaleClaimCandidate:
        self._validate_account_truth(claim, account_truth)
        process_result, process = await fetch_run_process(claim.run_id)
        self._validate_process_proof(claim, process_result, process)
        self._validate_binding_proof(claim)
        broker_proof = self._broker_proof(claim, account_truth.positions)
        return LegacyStaleClaimCandidate(
            claim_id=claim.claim_id,
            strategy_instance_id=claim.strategy_instance_id,
            run_id=claim.run_id,
            bot_order_namespace=claim.bot_order_namespace,
            symbol=claim.symbol,
            claimed_quantity=claim.claimed_quantity,
            proof_summary=broker_proof,
            proved_at_ms=self._now_ms(),
            confirmation=OperatorConfirmationCopy(
                title="Retire legacy stale claim",
                body="Retire the exact legacy claim that the backend has freshly proved safe to remove.",
                consequence="The server will append a durable retirement receipt and exclude only this legacy claim.",
                confirm_label="Retire stale claim",
            ),
        )

    def _validate_account_truth(self, claim: LegacyStaleClaim, account_truth: AccountTruthResponse) -> None:
        observed_account_id = account_truth.account_id or account_truth.health.account_id
        if observed_account_id is None or normalize_account_id(observed_account_id) != claim.account_id:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_ACCOUNT_TRUTH_ACCOUNT_MISMATCH",
                "Fresh broker evidence does not prove the requested account.",
            )
        position_source = next(
            (source for source in account_truth.source_freshness if source.source == "positions"),
            None,
        )
        if position_source is None or position_source.status != "fresh":
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_BROKER_POSITION_UNPROVEN",
                "Broker position evidence is missing or stale.",
            )

    def _validate_process_proof(
        self,
        claim: LegacyStaleClaim,
        result: DaemonResult,
        process: dict | None,
    ) -> None:
        if result.kind != "CONNECTED" or process is None:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_RUN_PROCESS_UNPROVEN",
                "The host daemon did not provide a current run-process proof.",
            )
        if process.get("run_id") != claim.run_id:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_RUN_PROCESS_UNPROVEN",
                "The host daemon did not return a terminal process record for this run.",
            )
        state = process.get("state")
        if state not in _SAFE_PROCESS_STATES:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_RUN_PROCESS_LIVE",
                f"Run {claim.run_id} has host process state {state!r}.",
            )

    def _validate_binding_proof(self, claim: LegacyStaleClaim) -> None:
        matching_bindings = [
            binding
            for binding in read_account_instance_registry(self._artifacts_root, claim.account_id)
            if binding.strategy_instance_id == claim.strategy_instance_id and binding.run_id == claim.run_id
        ]
        if not matching_bindings:
            return
        binding = max(matching_bindings, key=lambda row: row.recorded_at_ms)
        if binding.lifecycle_state != "RETIRED":
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_BINDING_ACTIVE",
                f"Binding for run {claim.run_id} is {binding.lifecycle_state}.",
            )
        if binding.bot_order_namespace != claim.bot_order_namespace:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_BINDING_NAMESPACE_MISMATCH",
                "The retired registry binding does not match the sidecar namespace.",
            )

    def _broker_proof(
        self,
        claim: LegacyStaleClaim,
        positions: Iterable[AccountTruthPositionRow],
    ) -> str:
        symbol_positions = [
            row for row in positions if row.symbol.upper() == claim.symbol and row.quantity != 0
        ]
        if not symbol_positions:
            return f"LEGACY_CLAIM_BROKER_FLAT:{claim.symbol}"
        if all(
            row.owner.owner_class in _KNOWN_ELSEWHERE_OWNER_CLASSES
            and row.owner.owner_key != claim.bot_order_namespace
            for row in symbol_positions
        ):
            owners = ",".join(sorted({row.owner.owner_key for row in symbol_positions}))
            return f"LEGACY_CLAIM_BROKER_ATTRIBUTED_ELSEWHERE:{claim.symbol}:{owners}"
        raise LegacyStaleClaimRetirementError(
            "LEGACY_CLAIM_BROKER_EXPOSURE_NOT_SAFE",
            f"Broker exposure for {claim.symbol} is unproven or still attributed to this legacy claim.",
        )

    def _find_claim(
        self,
        *,
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
        symbol: str,
    ) -> LegacyStaleClaim:
        canonical_symbol = symbol.upper()
        claim = next(
            (
                candidate
                for candidate in self.claims_for_account(account_id)
                if candidate.strategy_instance_id == strategy_instance_id
                and candidate.run_id == run_id
                and candidate.symbol == canonical_symbol
            ),
            None,
        )
        if claim is None:
            raise LegacyStaleClaimRetirementError(
                "LEGACY_CLAIM_NOT_FOUND",
                "No active pre-Clerk sidecar claim matches the requested identity.",
            )
        return claim

    def _read_claim_ledger(self, run_id: str) -> LiveRunLedger | None:
        try:
            return read_ledger(self._artifacts_root / "live_runs" / run_id / "run_ledger.json")
        except (OSError, ValueError):
            return None

def retired_legacy_claim_keys(
    artifacts_root: Path,
    account_id: str,
) -> frozenset[tuple[str, str, str, str]]:
    """Fold receipt events into the exact claims hidden from legacy sidecar sums."""

    keys: set[tuple[str, str, str, str]] = set()
    for event in read_account_events(artifacts_root, account_id):
        if event.get("event_type") != LEGACY_STALE_CLAIM_RETIRED_EVENT:
            continue
        values = (
            event.get("strategy_instance_id"),
            event.get("run_id"),
            event.get("symbol"),
            event.get("bot_order_namespace"),
        )
        if all(isinstance(value, str) and value for value in values):
            strategy_instance_id, run_id, symbol, namespace = values
            keys.add((strategy_instance_id, run_id, symbol.upper(), namespace))
    return frozenset(keys)


__all__ = [
    "LEGACY_STALE_CLAIM_RETIRED_EVENT",
    "LegacyStaleClaim",
    "LegacyStaleClaimRetirementError",
    "LegacyStaleClaimRetirementService",
    "retired_legacy_claim_keys",
]
