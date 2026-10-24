"""The ticker shape a raw symbol must have at a wire or setting boundary, and a setting that lists several.

``app.schemas.broker_bots`` owns the wire models and imports this; the settings module (the import root) needs the same
rule to refuse a bad symbol list when it loads, before any model is built, so the rule lives in a leaf that imports
nothing of the application.

Canonical implementation: this file.
"""

from __future__ import annotations

import re

#: Canonical equity/ETF symbol shape, shared by every schema that accepts a raw ticker at a wire boundary (do not write
#: a second regex). ``app.schemas.fleet_history_batch`` reuses :func:`normalized_symbol` rather than defining its own.
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


def normalized_symbol(value: str) -> str:
    """Canonicalize a raw ticker under the shared symbol shape."""
    normalized = value.strip().upper()
    if SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError("symbol must start with a letter and contain only letters, numbers, periods, or hyphens")
    return normalized


def parse_symbol_list(raw: str, *, setting: str) -> tuple[str, ...]:
    """A comma-separated ``setting`` as symbols: upper-cased, repeats dropped, empty entries ignored; an empty value is none.

    A token that is not a ticker raises a ``ValueError`` naming ``setting`` and the token.
    """
    symbols: list[str] = []
    for token in raw.split(","):
        if not token.strip():
            continue
        try:
            symbol = normalized_symbol(token)
        except ValueError as exc:
            raise ValueError(f"{setting} names {token.strip()!r}: {exc}") from exc
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)
