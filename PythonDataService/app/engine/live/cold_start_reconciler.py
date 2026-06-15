"""Cold-start reconciliation — encodes the 7-step procedure of
docs/ibkr-paper-deployment-plan.md §16.1 Resolution 2.

Contract: ``verify(broker, sidecar) -> SafeToResume | Poisoned``.
The bot must run this on every cold start; if the result is
Poisoned, the engine writes a poisoned.flag and refuses to submit
new orders until the operator inspects the situation. There is no
offline path — failing to reach the broker is itself Poisoned.

Grown vertically via TDD; each cycle adds one outcome branch or
one side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.engine.live.live_state_sidecar import LiveStateSidecarRepo


class _BrokerProtocol(Protocol):
    """The narrow broker surface the reconciler is permitted to call.

    Notably absent: reqAllOpenOrders. Resolution 2 forbids that — the
    reconciler must query only via its namespaced orderRef /
    client_order_id.
    """

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]: ...
    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class SafeToResume:
    from_bar_ms: int
    recovered_fills: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class Poisoned:
    reason: str


ReconciliationResult = SafeToResume | Poisoned


class ShadowBrokerMismatchError(RuntimeError):
    """VCR-P3-G — raised when ``shadow_mode=True`` is requested but the
    broker argument is not ``NoSubmitBrokerAdapter``.

    The shadow contract (ADR 0002) is that a shadow strategy never
    reaches ``ib.placeOrder``. ``NoSubmitBrokerAdapter`` enforces that
    structurally (no code path submits). Passing a real broker (e.g.
    ``IbkrBrokerAdapter``) with ``shadow_mode=True`` would silently
    relax the structural invariant to a runtime flag — the kind of
    drift that the next refactor turns into a real-money order. Fail
    fast at the reconciler boundary instead.
    """


class ColdStartReconciler:
    def verify(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: LiveStateSidecarRepo,
        shadow_mode: bool = False,
        run_dir: Path | None = None,
    ) -> ReconciliationResult:
        if shadow_mode:
            self._assert_shadow_broker(broker)
        result = self._verify_inner(broker=broker, sidecar=sidecar, shadow_mode=shadow_mode)
        if isinstance(result, Poisoned) and run_dir is not None:
            _write_poisoned_flag(run_dir, result.reason, sidecar=sidecar)
        return result

    @staticmethod
    def _assert_shadow_broker(broker: object) -> None:
        """VCR-P3-G — structural invariant: ``shadow_mode=True`` requires
        a broker that structurally cannot submit.

        Accepted brokers:
          * ``NoSubmitBrokerAdapter`` — the production shadow adapter
            (ADR 0002, ``no_submit_broker_adapter.py``).
          * Any object whose class declares ``_shadow_safe = True`` —
            the opt-in escape hatch for in-memory test fakes that need
            to exercise shadow-mode branches without pulling in the
            full NoSubmit adapter.

        Refused: anything else, especially ``IbkrBrokerAdapter`` — the
        whole point of P3-G is that a real broker wired with
        ``shadow_mode=True`` is the kind of drift that the next
        refactor turns into a real-money order.

        The shadow adapter is imported locally so the reconciler
        module stays decoupled from the shadow adapter at the top
        level (the shadow adapter pulls in market-data deps the
        reconciler does not need).
        """
        from app.engine.live.no_submit_broker_adapter import NoSubmitBrokerAdapter

        if isinstance(broker, NoSubmitBrokerAdapter):
            return
        if getattr(broker, "_shadow_safe", False) is True:
            return
        raise ShadowBrokerMismatchError(
            "shadow_mode=True requires NoSubmitBrokerAdapter (or a class "
            f"declaring _shadow_safe=True); got {type(broker).__name__}. "
            "The shadow contract is structural (ADR 0002): only a no-submit "
            "broker guarantees that no code path reaches ib.placeOrder. "
            "Refusing to proceed."
        )

    def _verify_inner(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: LiveStateSidecarRepo,
        shadow_mode: bool,
    ) -> ReconciliationResult:
        from app.engine.live.live_state_sidecar import LiveStateSidecarCorruptError

        try:
            envelope = sidecar.read()
        except LiveStateSidecarCorruptError:
            return Poisoned(reason="sidecar_corrupt")
        assert envelope is not None  # cycle 1 happy path

        try:
            broker_orders = broker.open_orders_by_namespace(envelope.bot_order_namespace)
        except Exception:
            # Resolution 2: no broker connection, no verified resume.
            # We deliberately catch broadly because any exception path
            # from the broker call — connection refused, timeout,
            # auth failure — means we cannot distinguish a clean cold
            # start from one with hidden divergence.
            return Poisoned(reason="cannot_verify_offline")
        broker_order_ids = {order.get("client_order_id") for order in broker_orders}

        # Shadow strategies never submit; the namespace must be empty.
        if shadow_mode:
            if broker_order_ids:
                return Poisoned(reason="shadow_namespace_nonempty")
            return SafeToResume(from_bar_ms=envelope.last_processed_bar_ms)

        for order_id in broker_order_ids:
            if order_id not in envelope.submitted_orders:
                return Poisoned(reason="unexpected_order_at_broker")

        # For each expected-open order that the broker doesn't show as
        # open, look at executions before declaring it missing — the
        # crash may have happened between fill and flush, in which case
        # the broker has the execution and we record it.
        try:
            executions = broker.executions_for_namespace(
                envelope.bot_order_namespace, envelope.last_artifact_flush_ms
            )
        except Exception:
            # Same contract as the open-order query above: there is no
            # offline/unverified resume path. If the execution-history
            # query fails (timeout, disconnect, auth) we cannot
            # distinguish a clean cold start from one with hidden,
            # unflushed fills — so we poison rather than raise.
            return Poisoned(reason="cannot_verify_offline")
        # Group every execution by order id — a single order can report
        # multiple executions (partial fills / split executions, each
        # with its own exec_id) after the last flush. Collapsing to one
        # entry per order would drop earlier unflushed fills and
        # understate the recovered quantity/P&L on resume.
        executions_by_order_id: dict[object, list[dict[str, object]]] = {}
        for exec_record in executions:
            executions_by_order_id.setdefault(
                exec_record.get("client_order_id"), []
            ).append(exec_record)
        recovered_fills: list[dict[str, object]] = []
        for sidecar_order_id in envelope.submitted_orders:
            if sidecar_order_id in broker_order_ids:
                continue
            fills = executions_by_order_id.get(sidecar_order_id)
            if not fills:
                return Poisoned(reason="expected_order_missing_at_broker")
            for fill in fills:
                if fill.get("exec_id") not in envelope.known_exec_ids:
                    recovered_fills.append(fill)

        return SafeToResume(
            from_bar_ms=envelope.last_processed_bar_ms,
            recovered_fills=recovered_fills,
        )


def _write_poisoned_flag(
    run_dir: Path, reason: str, *, sidecar: LiveStateSidecarRepo
) -> None:
    """Write <run_dir>/poisoned.flag in the shared halt JSON format.

    Delegates to ``halt.write_poisoned_flag`` so the on-disk shape is
    the same JSON object the rest of the system reads: ``run.py``'s
    ``read_poisoned_flag`` (which parses it as JSON and surfaces the
    trigger/timestamp) and the live-runs status endpoint's
    ``_read_flag`` (which also expects JSON). Writing the bare reason
    string here would make both consumers treat the flag as corrupt —
    the next ``start`` would report a corrupted flag and the status
    endpoint would not surface the poison details.

    The granular cold-start reason (``sidecar_corrupt``,
    ``unexpected_order_at_broker``, ...) is carried in
    ``details["reason"]`` under the shared
    ``COLD_START_DIVERGENCE`` trigger. ``last_clean_bar_close_ms`` is
    the sidecar's last processed bar when readable, else ``0`` (a
    corrupt sidecar gives us no clean bar to anchor on).
    """
    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        now_ms_utc,
        write_poisoned_flag,
    )
    from app.engine.live.live_state_sidecar import LiveStateSidecarCorruptError

    last_clean_bar_close_ms = 0
    try:
        envelope = sidecar.read()
    except LiveStateSidecarCorruptError:
        envelope = None
    if envelope is not None:
        last_clean_bar_close_ms = envelope.last_processed_bar_ms

    halt_reason = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
        halted_at_ms=now_ms_utc(),
        last_clean_bar_close_ms=last_clean_bar_close_ms,
        details={"reason": reason},
    )
    write_poisoned_flag(run_dir, halt_reason)
