"""Every broker-port guard method refuses inside the intake fence, before contact.

``test_intake_fence.py`` proves the guard has a method for each protocol
method; that a method exists says nothing about its fence call. A guard that
forgot ``_assert_unfenced`` would forward a broker call from inside a fold's
dynamic scope and fail open. The methods are read from the protocols and the
guard classes, so a method added later is covered with no edit here.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.broker_port_guard import (
    BrokerCallUnderIntakeError,
    GuardedBrokerReadPort,
    GuardedBrokerTradePort,
    guard_broker_ports,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.contract.ports import BrokerActivityEvidencePort, BrokerReadPort, BrokerTradePort

# Sync members that answer from the port's own ledger or constants and reach no
# broker; the guard leaves them unfenced on purpose (their docstrings say so).
_UNFENCED_BY_DESIGN = frozenset(
    {"capabilities", "synthesized_option_fills", "synthesized_fill_anchors"}
)


class _ContactRecorder:
    """An inner port whose every attribute read is recorded as contact."""

    def __init__(self) -> None:
        self.contacts: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.contacts.append(name)

        def contact(*_args: object, **_kwargs: object) -> None:
            self.contacts.append(f"{name}()")

        return contact


def _async_names(*protocols: type[object]) -> list[str]:
    return sorted(
        {name for protocol in protocols for name, _ in inspect.getmembers(protocol, inspect.iscoroutinefunction)}
    )


def _sync_method_names(guard: type[object]) -> list[str]:
    return sorted(
        name
        for name, member in vars(guard).items()
        if not name.startswith("_")
        and inspect.isfunction(member)
        and not inspect.iscoroutinefunction(member)
        and name not in _UNFENCED_BY_DESIGN
    )


def _required_arguments(guard: type[object], name: str) -> dict[str, object]:
    """A throwaway value for every required parameter; the guard refuses before it reads one."""
    signature = inspect.signature(getattr(guard, name))
    return {
        parameter.name: object()
        for parameter in signature.parameters.values()
        if parameter.name != "self"
        and parameter.default is inspect.Parameter.empty
        and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    }


_ASYNC_READ = _async_names(BrokerReadPort, BrokerActivityEvidencePort)
_ASYNC_TRADE = _async_names(BrokerTradePort)
_SYNC = [
    (guard, name)
    for guard in (GuardedBrokerReadPort, GuardedBrokerTradePort)
    for name in _sync_method_names(guard)
]

# A protocol that read as empty would turn the parametrized tests below vacuous.
assert _ASYNC_READ, "BrokerReadPort lists no async method"
assert _ASYNC_TRADE, "BrokerTradePort lists no async method"
assert _SYNC, "the guards list no sync method"


def _refusal(name: str) -> str:
    return rf"\b{re.escape(name)}\b"


@pytest.mark.parametrize("name", _ASYNC_READ)
async def test_every_async_read_port_method_refuses_under_the_fence_before_contact(name: str) -> None:
    fence = ReentrantAsyncLock()
    inner = _ContactRecorder()
    read, _trade = guard_broker_ports(read=inner, trade=_ContactRecorder(), intake=fence)  # type: ignore[arg-type]
    call = getattr(read, name)

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match=_refusal(name)):
            await call(**_required_arguments(GuardedBrokerReadPort, name))

    assert inner.contacts == []


@pytest.mark.parametrize("name", _ASYNC_TRADE)
async def test_every_async_trade_port_method_refuses_under_the_fence_before_contact(name: str) -> None:
    fence = ReentrantAsyncLock()
    inner = _ContactRecorder()
    _read, trade = guard_broker_ports(read=_ContactRecorder(), trade=inner, intake=fence)  # type: ignore[arg-type]
    call = getattr(trade, name)

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match=_refusal(name)):
            await call(**_required_arguments(GuardedBrokerTradePort, name))

    assert inner.contacts == []


@pytest.mark.parametrize(("guard", "name"), _SYNC, ids=[f"{g.__name__}.{n}" for g, n in _SYNC])
async def test_every_sync_port_method_refuses_under_the_fence_before_contact(
    guard: type[object], name: str
) -> None:
    """The ``bind_*`` capabilities, ``recovery_price_available`` and ``settle_expired_options``."""
    fence = ReentrantAsyncLock()
    inner = _ContactRecorder()
    read, trade = guard_broker_ports(read=inner, trade=inner, intake=fence)  # type: ignore[arg-type]
    call = getattr(read if guard is GuardedBrokerReadPort else trade, name)

    async with fence:
        with pytest.raises(BrokerCallUnderIntakeError, match=_refusal(name)):
            call(**_required_arguments(guard, name))

    assert inner.contacts == []
