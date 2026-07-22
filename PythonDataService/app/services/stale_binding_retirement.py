"""Proof-required retirement of stale account-instance registry bindings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from app.engine.live.account_artifacts import AccountArtifactError, append_account_event
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    index_account_instance_bindings,
    read_account_instance_registry,
)
from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.order_identity import order_ref_namespace_matches
from app.schemas.account_reconciliation import (
    StaleBindingRetirementCandidate,
    StaleBindingRetirementReceipt,
)
from app.schemas.account_truth import AccountTruthResponse
from app.schemas.live_runs import HostRunnerProcessStatus
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.utils.timestamps import now_ms_utc

_TERMINAL_PROCESS_STATES = frozenset({"idle", "exited"})
_STALE_BINDING_RETIREMENT_SOURCE = "operator.stale_binding_retirement"
RunProcessFetcher = Callable[
    [str],
    Awaitable[tuple[DaemonResult, HostRunnerProcessStatus | None]],
]
BindingRetirer = Callable[[str, str, str], Awaitable[AccountInstanceBinding]]


class StaleBindingRetirementError(AccountArtifactError):
    """A stale-binding retirement proof was absent or contradictory."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


class StaleBindingRetirementService:
    """Prove and retire a registry row without inventing a replacement binding."""

    def __init__(self, *, artifacts_root: Path, now_ms: Callable[[], int] = now_ms_utc) -> None:
        self._artifacts_root = artifacts_root
        self._now_ms = now_ms

    async def candidates(
        self,
        *,
        account_id: str,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
    ) -> list[StaleBindingRetirementCandidate]:
        """Return only active rows with fresh broker-flat and terminal-process proof."""

        canonical_account_id = normalize_account_id(account_id)
        self._validate_broker_flat(canonical_account_id, account_truth)
        bindings = index_account_instance_bindings(
            read_account_instance_registry(self._artifacts_root, canonical_account_id),
            account_id=canonical_account_id,
        ).latest_by_instance.values()
        candidates: list[StaleBindingRetirementCandidate] = []
        for binding in bindings:
            if binding.lifecycle_state not in {"DEPLOYED", "ACTIVE"}:
                continue
            try:
                candidates.append(
                    await self._prove_binding(
                        binding=binding,
                        account_truth=account_truth,
                        fetch_run_process=fetch_run_process,
                    )
                )
            except StaleBindingRetirementError:
                continue
        return candidates

    async def retire(
        self,
        *,
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
        requested_by: str,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
        retire_binding: BindingRetirer,
    ) -> StaleBindingRetirementReceipt:
        """Re-prove one binding, then ask the host authority to retire it atomically."""

        canonical_account_id = normalize_account_id(account_id)
        self._validate_broker_flat(canonical_account_id, account_truth)
        binding = self._current_binding(
            account_id=canonical_account_id,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        )
        self._validate_binding_open_orders(binding, account_truth)
        await self._verify_terminal_process(binding=binding, fetch_run_process=fetch_run_process)
        if binding.lifecycle_state == "RETIRED":
            if binding.source != _STALE_BINDING_RETIREMENT_SOURCE:
                raise StaleBindingRetirementError(
                    "STALE_BINDING_NOT_ACTIVE",
                    "The requested binding was retired by a different lifecycle action.",
                )
            return self._write_receipt(binding, requested_by=requested_by)
        retired = await retire_binding(canonical_account_id, strategy_instance_id, run_id)
        if (
            retired.account_id != canonical_account_id
            or retired.strategy_instance_id != strategy_instance_id
            or retired.run_id != run_id
            or retired.lifecycle_state != "RETIRED"
            or retired.source != _STALE_BINDING_RETIREMENT_SOURCE
        ):
            raise StaleBindingRetirementError(
                "STALE_BINDING_RETIRE_RESPONSE_INVALID",
                "The host did not attest retirement of the requested binding.",
            )
        return self._write_receipt(retired, requested_by=requested_by)

    def _write_receipt(
        self,
        binding: AccountInstanceBinding,
        *,
        requested_by: str,
    ) -> StaleBindingRetirementReceipt:
        """Persist (or recover) the receipt for one host-retired binding."""

        receipt = StaleBindingRetirementReceipt(
            receipt_id=(
                f"stale-binding-retirement:{binding.account_id}:{binding.strategy_instance_id}:{binding.run_id}"
            ),
            account_id=binding.account_id,
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            bot_order_namespace=binding.bot_order_namespace,
            requested_by=requested_by,
            retired_at_ms=binding.recorded_at_ms,
            source=binding.source,
        )
        append_account_event(
            self._artifacts_root,
            binding.account_id,
            {
                "event_type": "account_stale_binding_retired",
                "receipt_id": receipt.receipt_id,
                "strategy_instance_id": receipt.strategy_instance_id,
                "run_id": receipt.run_id,
                "bot_order_namespace": receipt.bot_order_namespace,
                "requested_by": receipt.requested_by,
                "recorded_at_ms": receipt.retired_at_ms,
                "source": receipt.source,
            },
            only_if_receipt_absent=True,
        )
        return receipt

    def _current_binding(
        self,
        *,
        account_id: str,
        strategy_instance_id: str,
        run_id: str,
    ) -> AccountInstanceBinding:
        binding = index_account_instance_bindings(
            read_account_instance_registry(self._artifacts_root, account_id),
            account_id=account_id,
        ).latest_by_instance.get(strategy_instance_id)
        if binding is None or binding.run_id != run_id:
            raise StaleBindingRetirementError(
                "STALE_BINDING_NOT_CURRENT",
                "The requested binding is no longer the account registry's current row.",
            )
        if binding.lifecycle_state not in {"DEPLOYED", "ACTIVE", "RETIRED"}:
            raise StaleBindingRetirementError(
                "STALE_BINDING_NOT_ACTIVE",
                "The requested binding is already retired or cannot be safely retired.",
            )
        return binding

    async def _prove_binding(
        self,
        *,
        binding: AccountInstanceBinding,
        account_truth: AccountTruthResponse,
        fetch_run_process: RunProcessFetcher,
    ) -> StaleBindingRetirementCandidate:
        self._validate_binding_open_orders(binding, account_truth)
        process = await self._verify_terminal_process(
            binding=binding,
            fetch_run_process=fetch_run_process,
        )
        return StaleBindingRetirementCandidate(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            bot_order_namespace=binding.bot_order_namespace,
            lifecycle_state=binding.lifecycle_state,
            source=binding.source,
            proof_summary=f"STALE_BINDING_BROKER_FLAT_AND_PROCESS_{process.state.upper()}",
            proved_at_ms=self._now_ms(),
            confirmation=OperatorConfirmationCopy(
                title="Retire stale deployment binding",
                body="Retire the exact inactive binding the backend has freshly proved safe to remove.",
                consequence="The host engine will append a RETIRED binding while preserving the full audit history.",
                confirm_label="Retire stale binding",
            ),
        )

    async def _verify_terminal_process(
        self,
        *,
        binding: AccountInstanceBinding,
        fetch_run_process: RunProcessFetcher,
    ) -> HostRunnerProcessStatus:
        result, process = await fetch_run_process(binding.run_id)
        if result.kind != "CONNECTED" or process is None:
            raise StaleBindingRetirementError(
                "STALE_BINDING_PROCESS_UNPROVEN",
                "The host daemon did not provide a current process proof for this binding.",
            )
        if process.run_id != binding.run_id:
            raise StaleBindingRetirementError(
                "STALE_BINDING_PROCESS_UNPROVEN",
                "The host daemon returned process proof for a different run.",
            )
        if process.state not in _TERMINAL_PROCESS_STATES:
            raise StaleBindingRetirementError(
                "STALE_BINDING_PROCESS_LIVE",
                "The host daemon still reports this binding as active or stopping.",
            )
        return process

    @staticmethod
    def _validate_broker_flat(account_id: str, account_truth: AccountTruthResponse) -> None:
        observed_account_id = account_truth.account_id or account_truth.health.account_id
        if observed_account_id is None or normalize_account_id(observed_account_id) != account_id:
            raise StaleBindingRetirementError(
                "STALE_BINDING_ACCOUNT_TRUTH_ACCOUNT_MISMATCH",
                "Fresh broker evidence does not prove the requested account.",
            )
        positions = next(
            (source for source in account_truth.source_freshness if source.source == "positions"),
            None,
        )
        if positions is None or positions.status != "fresh":
            raise StaleBindingRetirementError(
                "STALE_BINDING_BROKER_POSITION_UNPROVEN",
                "Broker position evidence is missing or stale.",
            )
        open_orders = next(
            (source for source in account_truth.source_freshness if source.source == "open_orders"),
            None,
        )
        if open_orders is None or open_orders.status != "fresh":
            raise StaleBindingRetirementError(
                "STALE_BINDING_BROKER_OPEN_ORDERS_UNPROVEN",
                "Broker open-order evidence is missing or stale.",
            )
        if any(position.quantity != 0 for position in account_truth.positions):
            raise StaleBindingRetirementError(
                "STALE_BINDING_BROKER_NOT_FLAT",
                "The broker account is not flat, so stale deployment bindings cannot be retired.",
            )

    @staticmethod
    def _validate_binding_open_orders(
        binding: AccountInstanceBinding,
        account_truth: AccountTruthResponse,
    ) -> None:
        if any(
            order.fact_kind == "open_order"
            and order_ref_namespace_matches(order.order_ref, {binding.bot_order_namespace})
            for order in account_truth.orders
        ):
            raise StaleBindingRetirementError(
                "STALE_BINDING_BROKER_OPEN_ORDER_LIVE",
                "A working broker order still belongs to the binding being retired.",
            )
