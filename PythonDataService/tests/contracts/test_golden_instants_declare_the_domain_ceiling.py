"""Every instant a Golden Search or Golden Validation wire schema carries declares the domain's range (ADR 0022).

``temporal-rigor.md``: every ``*_ms`` schema field is bounded to
``[0, MAX_TIMESTAMP_MS]``, so a consumer reading the contract knows the one
range an instant can hold and a value outside it is refused at the boundary.
The walk reads the live app's contract, so a field added later without its
bound fails here, not in review.
"""

from __future__ import annotations

from typing import Any

from app.main import app
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# A length of time in milliseconds, not an instant: it has no calendar range to declare.
DURATIONS = frozenset({"bar_span_ms"})


def _integer_bounds(prop: dict[str, Any]) -> list[tuple[Any, Any]]:
    """The (minimum, maximum) of every integer a property can hold: itself, an optional's member, or a list's item."""
    holders = [prop, *prop.get("anyOf", []), prop.get("items", {})]
    return [(holder.get("minimum"), holder.get("maximum")) for holder in holders if holder.get("type") == "integer"]


def test_every_golden_instant_field_is_bounded_to_the_admissible_range() -> None:
    schemas = app.openapi()["components"]["schemas"]
    instants = [
        (name, field, _integer_bounds(prop))
        for name, schema in schemas.items()
        if name.startswith("Golden")
        for field, prop in schema.get("properties", {}).items()
        if field.endswith("_ms") and field not in DURATIONS
    ]

    unbounded = [f"{name}.{field}" for name, field, bounds in instants if not bounds or any(bound != (0, MAX_TIMESTAMP_MS) for bound in bounds)]

    assert len(instants) > 50, "found almost no instant fields; the walk is broken, not the contract"
    assert unbounded == []
