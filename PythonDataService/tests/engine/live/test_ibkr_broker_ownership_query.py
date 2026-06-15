"""Phase 5C / VCR-0002 — IbkrBrokerOwnershipQuery tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.engine.live.broker_ownership_query import VerifiedBrokerOwnershipQuery
from app.engine.live.ibkr_broker_ownership_query import (
    VERIFIED_ORDER_REF_CAP,
    IbkrBrokerOwnershipQuery,
    _namespace_owns,
)


@dataclass
class _FakeOrder:
    orderId: int = 0
    permId: int = 0
    clientId: int = 1
    orderRef: str = ""
    action: str = "BUY"
    totalQuantity: float = 0.0


@dataclass
class _FakeOrderStatus:
    status: str = "Submitted"
    remaining: float = 0.0


@dataclass
class _FakeContract:
    symbol: str = "SPY"
    secType: str = "STK"
    conId: int = 12345


@dataclass
class _FakeTrade:
    order: _FakeOrder
    contract: _FakeContract = field(default_factory=_FakeContract)
    orderStatus: _FakeOrderStatus = field(default_factory=_FakeOrderStatus)


@dataclass
class _FakeExecution:
    execId: str = ""
    permId: int = 0
    orderId: int = 0
    clientId: int = 1
    acctNumber: str = "DU123"
    orderRef: str = ""
    side: str = "BOT"
    shares: float = 0.0
    price: float = 0.0
    time: datetime = field(default_factory=lambda: datetime(2026, 5, 4, 14, 30, tzinfo=UTC))


@dataclass
class _FakeCommissionReport:
    commission: float = 1.0


@dataclass
class _FakeFill:
    execution: _FakeExecution
    contract: _FakeContract = field(default_factory=_FakeContract)
    commissionReport: _FakeCommissionReport | None = field(default_factory=_FakeCommissionReport)


@dataclass
class _FakeIB:
    _open_trades: list[_FakeTrade] = field(default_factory=list)
    _fills: list[_FakeFill] = field(default_factory=list)

    def openTrades(self) -> list[Any]:
        return list(self._open_trades)

    def fills(self) -> list[Any]:
        return list(self._fills)


class _FakeClient:
    def __init__(self, ib: _FakeIB) -> None:
        self.ib = ib


def test_subclass_satisfies_verified_marker_vcr_0002() -> None:
    """Acceptance Gate #2 structural half: ``isinstance`` against the
    positive-allowlist base must succeed."""
    query = IbkrBrokerOwnershipQuery(_FakeClient(_FakeIB()))  # type: ignore[arg-type]
    assert isinstance(query, VerifiedBrokerOwnershipQuery)


def test_verified_order_ref_cap_matches_order_identity_default() -> None:
    from app.engine.live.order_identity import DEFAULT_ORDER_REF_MAX_LENGTH

    assert VERIFIED_ORDER_REF_CAP == DEFAULT_ORDER_REF_MAX_LENGTH


@pytest.mark.parametrize(
    "order_ref, namespace, expected",
    [
        ("learn-ai/inst/v1:abc", "learn-ai/inst/v1", True),
        ("learn-ai/inst/v10:abc", "learn-ai/inst/v1", False),
        ("learn-ai/other/v1:abc", "learn-ai/inst/v1", False),
        ("", "learn-ai/inst/v1", False),
        (None, "learn-ai/inst/v1", False),
        ("learn-ai/inst/v1xtra:abc", "learn-ai/inst/v1", False),
    ],
)
def test_namespace_owns_is_exact_prefix(
    order_ref: str | None, namespace: str, expected: bool
) -> None:
    assert _namespace_owns(order_ref, namespace) is expected


def test_open_orders_filtered_by_namespace_prefix() -> None:
    ns = "learn-ai/inst/v1"
    other_ns = "learn-ai/other/v1"
    ib = _FakeIB(
        _open_trades=[
            _FakeTrade(order=_FakeOrder(orderId=1, permId=101, orderRef=f"{ns}:aaa")),
            _FakeTrade(order=_FakeOrder(orderId=2, permId=102, orderRef=f"{other_ns}:bbb")),
            _FakeTrade(order=_FakeOrder(orderId=3, permId=103, orderRef="")),
            _FakeTrade(order=_FakeOrder(orderId=4, permId=104, orderRef=f"{ns}:ccc")),
        ]
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]

    rows = query.open_orders_by_namespace(ns)

    assert [r["order_id"] for r in rows] == [1, 4]
    assert {r["order_ref"] for r in rows} == {f"{ns}:aaa", f"{ns}:ccc"}


def test_open_orders_skip_version_leak() -> None:
    ib = _FakeIB(
        _open_trades=[
            _FakeTrade(order=_FakeOrder(orderId=1, orderRef="learn-ai/inst/v10:abc"))
        ]
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]
    assert query.open_orders_by_namespace("learn-ai/inst/v1") == []


def test_executions_filtered_by_namespace_prefix() -> None:
    ns = "learn-ai/inst/v1"
    t = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    ib = _FakeIB(
        _fills=[
            _FakeFill(
                execution=_FakeExecution(
                    execId="e1", permId=101, orderId=1, orderRef=f"{ns}:aaa", time=t
                )
            ),
            _FakeFill(
                execution=_FakeExecution(
                    execId="e2",
                    permId=102,
                    orderId=2,
                    orderRef="learn-ai/other/v1:bbb",
                    time=t,
                )
            ),
        ]
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]
    rows = query.executions_for_namespace(ns, since_ms=0)
    assert [r["exec_id"] for r in rows] == ["e1"]


def test_executions_use_perm_id_fallback_when_execution_lacks_order_ref() -> None:
    """When Execution.orderRef is empty (the PRD-noted gotcha), the
    classifier cross-references via permId against the open trades cache."""
    ns = "learn-ai/inst/v1"
    t = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    ib = _FakeIB(
        _open_trades=[
            _FakeTrade(order=_FakeOrder(orderId=1, permId=999, orderRef=f"{ns}:aaa"))
        ],
        _fills=[
            _FakeFill(
                execution=_FakeExecution(
                    execId="e1", permId=999, orderId=1, orderRef="", time=t
                )
            )
        ],
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]
    rows = query.executions_for_namespace(ns, since_ms=0)
    assert len(rows) == 1
    assert rows[0]["exec_id"] == "e1"
    assert rows[0]["order_ref"] == f"{ns}:aaa"


def test_executions_skipped_when_perm_id_not_in_open_trades() -> None:
    ns = "learn-ai/inst/v1"
    t = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    ib = _FakeIB(
        _open_trades=[],
        _fills=[
            _FakeFill(
                execution=_FakeExecution(execId="e1", permId=999, orderRef="", time=t)
            )
        ],
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]
    assert query.executions_for_namespace(ns, since_ms=0) == []


def test_executions_honor_since_ms_floor() -> None:
    ns = "learn-ai/inst/v1"
    pre_session = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    in_session = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)
    session_start_ms = int(datetime(2026, 5, 4, 14, 0, tzinfo=UTC).timestamp() * 1000)
    ib = _FakeIB(
        _fills=[
            _FakeFill(
                execution=_FakeExecution(
                    execId="pre", permId=1, orderRef=f"{ns}:a", time=pre_session
                )
            ),
            _FakeFill(
                execution=_FakeExecution(
                    execId="now", permId=2, orderRef=f"{ns}:b", time=in_session
                )
            ),
        ]
    )
    query = IbkrBrokerOwnershipQuery(_FakeClient(ib))  # type: ignore[arg-type]
    rows = query.executions_for_namespace(ns, since_ms=session_start_ms)
    assert [r["exec_id"] for r in rows] == ["now"]


def test_require_durable_submit_activation_passes_with_subclass() -> None:
    """Full activation contract: subclass + verified cap + enabled=True
    → no exception. After paper-side validation the operator flips this on."""
    from app.engine.live.broker_ownership_query import (
        require_durable_submit_activation,
    )

    query = IbkrBrokerOwnershipQuery(_FakeClient(_FakeIB()))  # type: ignore[arg-type]
    require_durable_submit_activation(
        enabled=True,
        verified_order_ref_cap=VERIFIED_ORDER_REF_CAP,
        ownership_query=query,
    )


def test_require_durable_submit_activation_refuses_fail_closed_default() -> None:
    from app.engine.live.broker_ownership_query import (
        DurableSubmitNotActivatable,
        FailClosedBrokerOwnershipQuery,
        require_durable_submit_activation,
    )

    with pytest.raises(DurableSubmitNotActivatable, match="ownership query unverified"):
        require_durable_submit_activation(
            enabled=True,
            verified_order_ref_cap=VERIFIED_ORDER_REF_CAP,
            ownership_query=FailClosedBrokerOwnershipQuery(),
        )
