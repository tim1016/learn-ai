"""Dev-only broker fault-injection seam (PRD #1354).

The seam lets a rehearsal deliberately trigger reject / conflict / throttle /
timeout (write path) and redelivery / halt / partial-fill (trade-updates frame
path) scenarios against the **real** Alpaca adapter and the **real** live
consumer — so the operator can prove the Clerk/instance authority boundary and
the panel's UI feedback under adverse conditions.

Safety is structural, not a promise:

* **Off by default and paper-only.** :func:`injection_permitted` requires the
  ``ALPACA_FAULT_INJECTION_ENABLED`` flag *and* a paper Alpaca posture. Arming
  is refused otherwise; every consume hook is inert otherwise. Any error
  resolving the posture fails closed.
* **No abnormal order is ever placed.** A write fault simulates the *vendor
  response* and short-circuits **before** the SDK call, so no order reaches
  Alpaca. A frame fault simulates an *inbound frame*; it never constructs or
  submits an order. The seam can only fabricate a response or a frame.

Two hooks consume this registry:

* :mod:`app.broker.alpaca.client` (write path) — an armed write fault becomes a
  vendor-shaped :class:`~alpaca.common.exceptions.APIError` routed through the
  real :func:`~app.broker.alpaca.errors.map_api_error`, or the same
  ``BrokerUnavailable('timed out')`` a real timeout maps to.
* :mod:`app.broker.alpaca.trade_updates` (frame source) — an armed frame fault
  interleaves a crafted inbound frame through the real ``_handle_frame``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from alpaca.common.exceptions import APIError

from app.broker.alpaca.config import BROKER_ID, get_alpaca_settings
from app.broker.alpaca.errors import map_api_error
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.config import settings

logger = logging.getLogger(__name__)


class FaultInjectionRefused(Exception):
    """Arming was refused because the seam is not permitted (off or not paper)."""


class WriteFaultKind(StrEnum):
    """Vendor responses the write-path hook can simulate for a submit/cancel."""

    REJECT_422 = "reject_422"
    CONFLICT_409 = "conflict_409"
    THROTTLE_429 = "throttle_429"
    TIMEOUT = "timeout"


class FrameFaultKind(StrEnum):
    """Inbound ``trade_updates`` frames the frame-source hook can interleave."""

    REDELIVER_LAST = "redeliver_last"
    HALT_SUSPENDED = "halt_suspended"
    HALT_STOPPED = "halt_stopped"
    PARTIAL_FILL = "partial_fill"


_WRITE_KINDS = frozenset(kind.value for kind in WriteFaultKind)
_FRAME_KINDS = frozenset(kind.value for kind in FrameFaultKind)


def injection_permitted() -> bool:
    """True only when the seam is explicitly enabled AND the posture is paper.

    Fails closed on any error resolving the Alpaca posture (e.g. missing
    credentials) — an indeterminate posture is treated as not-paper.
    """
    if not settings.ALPACA_FAULT_INJECTION_ENABLED:
        return False
    try:
        return get_alpaca_settings().is_paper
    except Exception:
        # An unresolvable posture is not a paper posture — refuse.
        return False


@dataclass
class ArmedFault:
    """One armed fault: its kind, an optional target, and a remaining budget.

    ``target`` scopes the fault. For a write fault it is matched (substring)
    against the operation's identity — the submit ``client_order_id`` or the
    cancel ``order_id``; ``None`` matches any mutation. For a frame fault it
    supplies the ``client_order_id`` the crafted frame is attributed to;
    ``None`` leaves the crafted frame unattributed (an "unexplained order").

    ``params`` carries frame-shaping overrides (``symbol`` / ``qty`` / ``price``
    / ``execution_id``); it is unused for write faults.
    """

    kind: str
    target: str | None
    remaining: int
    params: dict[str, str] = field(default_factory=dict)

    def as_status(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "remaining": self.remaining,
            "params": dict(self.params),
        }


class FaultInjectionRegistry:
    """Process-local registry of armed faults. Thread-safe; inert unless permitted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._write: list[ArmedFault] = []
        self._frame: list[ArmedFault] = []

    # ── Arming surface ────────────────────────────────────────────────────────

    def arm(
        self,
        kind: str,
        *,
        target: str | None = None,
        count: int = 1,
        params: dict[str, str] | None = None,
    ) -> ArmedFault:
        """Arm one fault. Refuses unless the seam is permitted; validates inputs."""
        if not injection_permitted():
            raise FaultInjectionRefused(
                "Fault injection is not permitted: it requires "
                "ALPACA_FAULT_INJECTION_ENABLED and a paper Alpaca posture."
            )
        if count < 1:
            raise ValueError("count must be >= 1")
        fault = ArmedFault(kind=kind, target=target, remaining=count, params=dict(params or {}))
        with self._lock:
            if kind in _WRITE_KINDS:
                self._write.append(fault)
            elif kind in _FRAME_KINDS:
                self._frame.append(fault)
            else:
                raise ValueError(f"unknown fault kind: {kind!r}")
        logger.info(
            "fault injection armed",
            extra={
                "action": "fault_injection_armed",
                "kind": kind,
                "target": target,
                "count": count,
            },
        )
        return fault

    def disarm_all(self) -> int:
        """Clear every armed fault. Returns how many were cleared."""
        with self._lock:
            cleared = len(self._write) + len(self._frame)
            self._write.clear()
            self._frame.clear()
        if cleared:
            logger.info(
                "fault injection disarmed",
                extra={"action": "fault_injection_disarmed", "cleared": cleared},
            )
        return cleared

    def status(self) -> dict[str, Any]:
        """Snapshot of currently-armed faults and whether the seam is permitted."""
        with self._lock:
            return {
                "permitted": injection_permitted(),
                "write": [fault.as_status() for fault in self._write],
                "frame": [fault.as_status() for fault in self._frame],
            }

    # ── Consumption hooks ─────────────────────────────────────────────────────

    def consume_write_fault(self, identifier: str) -> ArmedFault | None:
        """Return (and spend) the first armed write fault matching ``identifier``.

        Inert (``None``) unless the seam is permitted. A fault with a ``target``
        matches when ``target`` is a substring of ``identifier``; a ``None``
        target matches any mutation. The matched fault's budget is decremented;
        it is removed once exhausted.
        """
        if not injection_permitted():
            return None
        with self._lock:
            for index, fault in enumerate(self._write):
                if fault.target is not None and fault.target not in identifier:
                    continue
                fault.remaining -= 1
                spent = ArmedFault(
                    kind=fault.kind,
                    target=fault.target,
                    remaining=fault.remaining,
                    params=dict(fault.params),
                )
                if fault.remaining <= 0:
                    del self._write[index]
                return spent
        return None

    def drain_frame_faults(self) -> list[ArmedFault]:
        """Return (and spend) all armed frame faults, one entry per remaining unit.

        Inert (empty) unless the seam is permitted. Each armed frame fault yields
        ``remaining`` copies so the caller crafts and injects that many frames,
        then the fault is cleared.
        """
        if not injection_permitted():
            return []
        drained: list[ArmedFault] = []
        with self._lock:
            for fault in self._frame:
                drained.extend(
                    ArmedFault(kind=fault.kind, target=fault.target, remaining=1, params=dict(fault.params))
                    for _ in range(fault.remaining)
                )
            self._frame.clear()
        return drained


_REGISTRY = FaultInjectionRegistry()


def get_fault_injection_registry() -> FaultInjectionRegistry:
    """Return the process-level fault-injection registry."""
    return _REGISTRY


def reset_fault_injection_for_testing() -> None:
    """Clear every armed fault (test isolation)."""
    _REGISTRY.disarm_all()


# ── Crafting: vendor responses (write path) ───────────────────────────────────


def _api_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> APIError:
    """Build a vendor-shaped ``APIError`` the real ``map_api_error`` can classify."""
    body = json.dumps({"code": 40010000, "message": message})
    response = SimpleNamespace(status_code=status, headers=headers or {})
    http_error = SimpleNamespace(response=response, request=None)
    return APIError(body, http_error=http_error)


def craft_write_error(kind: str) -> BrokerError:
    """Translate a write-fault kind into the contract error the real path yields.

    Reject / conflict / throttle route through the real ``map_api_error`` so the
    contract error is byte-identical to a genuine vendor failure (and the Clerk
    classifies it identically). Timeout returns the same ``BrokerUnavailable``
    the real ``anyio.fail_after`` path produces, so the uncertain-submission
    handling is exercised without a real 15 s stall.
    """
    if kind == WriteFaultKind.REJECT_422:
        return map_api_error(
            _api_error(422, "injected order rejection"),
            broker=BROKER_ID,
            is_order_mutation=True,
        )
    if kind == WriteFaultKind.CONFLICT_409:
        return map_api_error(
            _api_error(409, "injected order conflict"),
            broker=BROKER_ID,
            is_order_mutation=True,
        )
    if kind == WriteFaultKind.THROTTLE_429:
        return map_api_error(
            _api_error(429, "injected rate limit", headers={"Retry-After": "0"}),
            broker=BROKER_ID,
            is_order_mutation=True,
        )
    if kind == WriteFaultKind.TIMEOUT:
        return BrokerUnavailable(
            "Alpaca timed out while submitting the order (injected).",
            broker=BROKER_ID,
            detail="Injected submit timeout; the outcome is deliberately uncertain.",
        )
    raise ValueError(f"not a write fault kind: {kind!r}")


# ── Crafting: inbound trade-updates frames (frame path) ───────────────────────

_DEFAULT_SYMBOL = "SPY"
_DEFAULT_QTY = "1"
_DEFAULT_PRICE = "100.00"
# A stable ET-anchored wire timestamp; frames carry vendor RFC3339 strings, and
# the real parser converts to int64 ms UTC at ingestion.
_INJECTED_AT = "2026-08-04T14:42:49.354133Z"


def _base_order(
    *,
    client_order_id: str | None,
    symbol: str,
    status: str,
    filled_qty: str,
    qty: str,
    filled_avg_price: str | None,
) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-0000000f1nj0",
        "client_order_id": client_order_id,
        "created_at": _INJECTED_AT,
        "updated_at": _INJECTED_AT,
        "submitted_at": _INJECTED_AT,
        "filled_at": _INJECTED_AT if status in ("filled", "partially_filled") else None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "asset_id": "00000000-0000-0000-0000-0000000a55e7",
        "symbol": symbol,
        "asset_class": "us_equity",
        "notional": None,
        "qty": qty,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "order_class": "",
        "order_type": "market",
        "type": "market",
        "side": "buy",
        "position_intent": "buy_to_open",
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "status": status,
    }


# kind → (wire event, order status, whether it carries a fill execution slice).
_CRAFTABLE_FRAME_SPECS: dict[str, tuple[str, str, bool]] = {
    FrameFaultKind.PARTIAL_FILL: ("partial_fill", "partially_filled", True),
    FrameFaultKind.HALT_SUSPENDED: ("suspended", "suspended", False),
    FrameFaultKind.HALT_STOPPED: ("stopped", "stopped", False),
}


def craft_frame(fault: ArmedFault) -> str:
    """Serialize one crafted ``trade_updates`` frame for a frame fault.

    Mirrors the committed fixture shape so the real ``_handle_frame`` parses,
    dedups, and attributes it exactly as a socket frame. ``REDELIVER_LAST`` is
    handled by the frame-source hook (it re-emits the last real frame) and is
    not crafted here.
    """
    spec = _CRAFTABLE_FRAME_SPECS.get(fault.kind)
    if spec is None:
        raise ValueError(f"not a craftable frame fault kind: {fault.kind!r}")
    event, order_status, carries_fill = spec

    params = fault.params
    symbol = params.get("symbol") or _DEFAULT_SYMBOL
    qty = params.get("qty") or _DEFAULT_QTY
    price = params.get("price") or _DEFAULT_PRICE

    data: dict[str, Any] = {
        "at": _INJECTED_AT,
        "event_id": f"01INJECTED{fault.kind}",
        "event": event,
        "timestamp": _INJECTED_AT,
        "order": _base_order(
            client_order_id=fault.target,
            symbol=symbol,
            status=order_status,
            filled_qty=qty if carries_fill else "0",
            qty=(params.get("order_qty") or "10") if carries_fill else qty,
            filled_avg_price=price if carries_fill else None,
        ),
    }
    if carries_fill:
        # Fills carry the execution slice at the top level (qty/price/execution_id),
        # exactly where from_alpaca_trade_update reads it.
        data["execution_id"] = params.get("execution_id") or "injected-exec-1"
        data["qty"] = qty
        data["price"] = price

    return json.dumps({"stream": "trade_updates", "data": data})


def frame_for_fault(fault: ArmedFault, *, last_frame: str | None) -> str | None:
    """Resolve a frame fault to the wire frame to inject, or ``None`` to skip.

    ``REDELIVER_LAST`` re-emits the last real ``trade_updates`` frame the stream
    delivered (``None`` when none has arrived yet — nothing to redeliver). Every
    other frame fault is crafted from a template.
    """
    if fault.kind == FrameFaultKind.REDELIVER_LAST:
        return last_frame
    return craft_frame(fault)
