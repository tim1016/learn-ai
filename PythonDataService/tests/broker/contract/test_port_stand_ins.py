"""Every class in the app that stands in for a broker port has all of the port's async methods.

A port is a ``Protocol``. A class that lacks one of its methods still passes
as the port until that method is called, and then fails with
``AttributeError`` where the port promised an answer or a typed refusal. The
stand-ins are found by reading the app's source, so one added later is held
to the port without being listed here.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import app
from app.broker.alpaca.clerk.sqlite.broker_port_guard import missing_guarded_async_methods
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

_APP = Path(app.__file__).parent

# A class that defines both of these itself is standing in for the port, whatever else it lacks.
_TRADE_PORT_MARKS = frozenset({"submit", "get_order_by_client_order_id"})
_READ_PORT_MARKS = frozenset({"get_account", "get_clock_evidence"})


def _stand_ins(marks: frozenset[str]) -> list[type]:
    """Each module-level class under ``app/`` that defines every method in ``marks`` as its own ``async def``."""
    found: list[type] = []
    for path in sorted(_APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not all(f"async def {name}(" in source for name in marks):
            continue
        module = ".".join(("app", *path.relative_to(_APP).with_suffix("").parts))
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and marks <= {
                member.name for member in node.body if isinstance(member, ast.AsyncFunctionDef)
            }:
                found.append(getattr(importlib.import_module(module), node.name))
    return found


@pytest.mark.parametrize(
    ("port", "marks", "known"),
    [
        pytest.param(
            BrokerTradePort,
            _TRADE_PORT_MARKS,
            {
                "AlpacaBroker",
                "GuardedBrokerTradePort",
                "NoSubmitAlpacaTradePort",
                "SyntheticBroker",
                # Wraps the guarded port for one stock leg; it must pass an option call on, not lack the method.
                "_PricingBarBoundTradePort",
                # Wraps it for one option entry, to bind the quote that entry is filled from.
                "_OptionQuoteBoundTradePort",
            },
            id="trade port",
        ),
        pytest.param(
            BrokerReadPort,
            _READ_PORT_MARKS,
            {"AlpacaBroker", "GuardedBrokerReadPort", "ShadowAccountReadPort", "SyntheticBroker"},
            id="read port",
        ),
    ],
)
def test_every_stand_in_for_a_port_has_all_of_the_ports_async_methods(
    port: type, marks: frozenset[str], known: set[str]
) -> None:
    stand_ins = _stand_ins(marks)

    # The search itself is held to the stand-ins this repo is known to have: one that finds nothing proves nothing.
    assert {stand_in.__name__ for stand_in in stand_ins} >= known
    lacking = {
        stand_in.__qualname__: sorted(missing)
        for stand_in in stand_ins
        if (missing := missing_guarded_async_methods(port, stand_in))
    }
    assert lacking == {}
