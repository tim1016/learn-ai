"""The one 400 for a fill mode a request may not ask for (#2599).

Every HTTP surface that takes a fill mode by name -- the engine backtest,
research runs, the legacy walk-forward route and spec strategies -- parses it
here and passes the returned mode, or its ``.value`` (the canonical name),
onward, so nothing runs, stores or hashes the request's own spelling. The
parser itself lives in ``app/engine``, which does not import FastAPI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fastapi import HTTPException, status

from app.engine.execution.fill_mode_names import (
    NO_ALIASES,
    REQUEST_FILL_MODES,
    UnknownFillModeError,
    parse_fill_mode,
)
from app.engine.execution.order import FillMode


def fill_mode_or_400(
    raw: str,
    *,
    aliases: Mapping[str, FillMode] = NO_ALIASES,
    detail: Callable[[UnknownFillModeError], str] = str,
) -> FillMode:
    """Return the mode a research request names, or refuse it with a 400 naming the modes it may ask for.

    ``aliases`` and ``detail`` are for the engine backtest, which alone keeps
    its short names and its own refusal text.
    """
    try:
        return parse_fill_mode(raw, allowed=REQUEST_FILL_MODES, aliases=aliases)
    except UnknownFillModeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail(exc)) from exc
