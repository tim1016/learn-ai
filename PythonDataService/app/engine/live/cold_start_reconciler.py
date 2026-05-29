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

import contextlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


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


class ColdStartReconciler:
    def verify(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: "LiveStateSidecarRepo",  # noqa: F821 — forward string ref
        shadow_mode: bool = False,
        run_dir: Path | None = None,
    ) -> ReconciliationResult:
        result = self._verify_inner(broker=broker, sidecar=sidecar, shadow_mode=shadow_mode)
        if isinstance(result, Poisoned) and run_dir is not None:
            _write_poisoned_flag(run_dir, result.reason)
        return result

    def _verify_inner(
        self,
        *,
        broker: _BrokerProtocol,
        sidecar: "LiveStateSidecarRepo",  # noqa: F821 — forward string ref
        shadow_mode: bool,
    ) -> ReconciliationResult:
        envelope = sidecar.read()
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
        executions = broker.executions_for_namespace(
            envelope.bot_order_namespace, envelope.last_artifact_flush_ms
        )
        executions_by_order_id: dict[object, dict[str, object]] = {
            exec_record.get("client_order_id"): exec_record for exec_record in executions
        }
        recovered_fills: list[dict[str, object]] = []
        for sidecar_order_id in envelope.submitted_orders:
            if sidecar_order_id in broker_order_ids:
                continue
            fill = executions_by_order_id.get(sidecar_order_id)
            if fill is None:
                return Poisoned(reason="expected_order_missing_at_broker")
            if fill.get("exec_id") not in envelope.known_exec_ids:
                recovered_fills.append(fill)

        return SafeToResume(
            from_bar_ms=envelope.last_processed_bar_ms,
            recovered_fills=recovered_fills,
        )


def _write_poisoned_flag(run_dir: Path, reason: str) -> None:
    """Atomically write <run_dir>/poisoned.flag with the reason.

    Mirrors the LiveStateSidecar atomic-write pattern: tempfile, fsync,
    os.replace, parent-dir fsync on POSIX. The flag is the cross-process
    signal to the engine and operator tooling that this run must not
    submit new orders. Atomicity matters because partial writes from a
    crash here would leave a half-written flag that downstream
    parsing might either miss or misinterpret.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "poisoned.flag"
    tmp = target.with_suffix(".flag.tmp")
    payload = reason.encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    if sys.platform != "win32":
        dir_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(dir_fd)
