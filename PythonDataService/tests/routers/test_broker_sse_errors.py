"""The broker SSE streams must not hand an exception's text to a client.

Every ``event: error`` frame these streams emit is authored here, not derived
from the caught exception. Serializing ``str(exc)`` gives an external caller
whatever an unexpected exception happens to carry (CodeQL
``py/stack-trace-exposure``) while telling the operator nothing the service
log does not already hold.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from app.routers.broker import _sse_error_frame

_BROKER_ROUTER = Path(__file__).resolve().parents[2] / "app" / "routers" / "broker.py"


def test_sse_error_frame_is_a_well_formed_single_event() -> None:
    frame = _sse_error_frame("The order-event stream stopped.")

    assert frame.startswith("event: error\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {"error": "The order-event stream stopped."}


def test_no_sse_payload_is_built_from_a_caught_exception() -> None:
    """Pin the whole router, so a new stream cannot reintroduce the leak.

    Scope is the SSE body only. Every stream payload here is serialized with
    ``json.dumps``, so a ``json.dumps`` call that reads a caught exception is
    the leak this guards. ``HTTPException(..., str(exc))`` is deliberately
    left alone: a typed ``BrokerError``'s text is the response's whole point,
    and those sites are request/response, not a long-lived stream.
    """
    tree = ast.parse(_BROKER_ROUTER.read_text(encoding="utf-8"))
    handler_names = {
        handler.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if handler.name is not None
    }
    assert handler_names, "expected the broker router to bind caught exceptions"

    leaked: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "dumps"):
            continue
        for descendant in ast.walk(node):
            if isinstance(descendant, ast.Name) and descendant.id in handler_names:
                leaked.append(node.lineno)

    assert leaked == [], (
        f"broker.py serializes a caught exception into an SSE payload at lines "
        f"{sorted(set(leaked))}; author the operator message instead and log the exception"
    )
