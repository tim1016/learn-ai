from __future__ import annotations

from typing import Any, Literal, get_args, get_origin


def get_literal_args(literal_type: Any) -> tuple[Any, ...]:
    """Return the args of a Literal type, asserting it is actually a Literal.

    Used by parametrized exhaustiveness tests. Centralized so the same
    helper covers Literals reached in PRs 1–5.
    """
    origin = get_origin(literal_type)
    if origin is not Literal:
        raise TypeError(
            f"get_literal_args expected a Literal, got {literal_type!r} "
            f"with origin {origin!r}"
        )
    return get_args(literal_type)
