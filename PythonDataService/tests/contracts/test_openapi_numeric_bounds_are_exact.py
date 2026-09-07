"""Every numeric bound in the published contract says what it means (#1936).

FastAPI's ``openapi.models.Schema`` types ``maximum`` / ``minimum`` as
``float``. A pydantic ``le=2**63 - 1`` therefore reached the committed
contract as ``9.223372036854776e+18`` — the nearest double, which is ``2**63``,
one *higher* than the bound actually enforced. Twenty-one fields published a
ceiling the service would reject, in the document whose entire job is to state
the contract exactly.

The fix was not to round-trip the number more carefully; ``2**63 - 1`` ms is
the year 292 million and was never anyone's real bound. Every timestamp field
now declares ``MAX_TIMESTAMP_MS`` — the end of year 9999, the largest instant
the domain admits — which is under ``2**53`` and so survives the float
round-trip exactly. Six hand-rolled copies of the old ceiling
(``INT64_MAX``, ``_INT64_MAX``, ``INT64_MS_MAX``, ``_MAX_INT64``,
``_MAX_INT64_MS``, ``MAX_EPOCH_MS``) collapsed into that one.

This test is the guard on the property, not on the number: any bound a JSON
consumer cannot read back as the integer we validate against is a defect,
whatever produced it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.utils.session_anchors import MAX_TIMESTAMP_MS

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "python-data-service.openapi.json"

# Above this, consecutive integers are no longer distinguishable in a float64,
# so a JSON number is no longer a faithful carrier of an integer bound.
#
# The check is on the published document, which is all a client has — so it
# cannot see the *declared* bound and compare. That leaves exactly one integer
# uncaught: a declared ``2**53 + 1`` publishes as ``9007199254740992.0`` and
# reads as within range. Catching it would take building ``app.openapi()`` and
# diffing each bound against its pydantic field, which is a different test than
# "is the committed contract honest". Noted rather than papered over.
EXACT_INTEGER_LIMIT = 2**53

BOUND_KEYS = ("maximum", "minimum", "exclusiveMaximum", "exclusiveMinimum")


def _bounds(node: Any, path: str) -> list[tuple[str, str, float]]:
    found: list[tuple[str, str, float]] = []
    if isinstance(node, dict):
        for key in BOUND_KEYS:
            value = node.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                found.append((path, key, value))
        for key, value in node.items():
            found.extend(_bounds(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_bounds(value, f"{path}[{index}]"))
    return found


def test_no_published_bound_is_beyond_float64s_exact_integers() -> None:
    document = json.loads(CONTRACT.read_text(encoding="utf-8"))
    bounds = _bounds(document, "")
    assert bounds, "found no numeric bounds at all; the walk is broken, not the contract"

    lossy = [(path, key, value) for path, key, value in bounds if abs(value) > EXACT_INTEGER_LIMIT]
    assert lossy == [], (
        "these bounds cannot be read back as the integer they constrain — a client "
        f"honouring them would accept values the service rejects: {lossy}"
    )


def test_the_domain_ceiling_survives_the_json_round_trip() -> None:
    """The property that makes ``MAX_TIMESTAMP_MS`` publishable at all."""
    published = json.loads(json.dumps(float(MAX_TIMESTAMP_MS)))
    assert int(published) == MAX_TIMESTAMP_MS
    assert MAX_TIMESTAMP_MS < EXACT_INTEGER_LIMIT


def test_the_domain_ceiling_is_the_end_of_year_9999() -> None:
    """A bound nobody can explain gets copied wrong; this one has an answer."""
    from datetime import UTC, datetime, timedelta

    # Integer arithmetic on purpose: ``/ 1000`` would reintroduce exactly the
    # float step this whole issue is about.
    assert datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=MAX_TIMESTAMP_MS) == datetime(
        9999, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC
    )
