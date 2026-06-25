"""Full-fidelity IBKR object evidence helpers.

The curated broker models expose stable fields the engine already consumes,
but audit and UI surfaces also need the complete ib_async payloads that IBKR
sent or that we submitted. This module converts ib_async dataclass-like
objects into JSON-safe, timestamp-rigorous snapshots without importing those
types at model-definition time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace
from typing import TypeAlias

IbkrEvidenceScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040
IbkrEvidenceValue: TypeAlias = (  # noqa: UP040
    IbkrEvidenceScalar | list["IbkrEvidenceValue"] | dict[str, "IbkrEvidenceValue"]
)

_MAX_DEPTH = 8


def snapshot_ibkr_object(obj: object | None) -> dict[str, IbkrEvidenceValue] | None:
    """Return all public fields on ``obj`` as JSON-safe evidence.

    ``None`` stays ``None`` so callers can distinguish "object absent" from
    "present object with no public fields". Dataclasses are expanded via
    ``asdict``; ib_async objects and test fakes are expanded via their public
    attributes.
    """
    if obj is None:
        return None
    value = _to_evidence_value(obj, depth=0)
    if not isinstance(value, dict):
        return {"value": value}
    return value


def ibkr_object_type(obj: object | None) -> str | None:
    if obj is None:
        return None
    cls = obj.__class__
    return f"{cls.__module__}.{cls.__qualname__}"


def _to_evidence_value(obj: object, *, depth: int) -> IbkrEvidenceValue:
    if depth > _MAX_DEPTH:
        return repr(obj)

    if obj is None or isinstance(obj, str | int | bool):
        return obj

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    if isinstance(obj, Decimal):
        return str(obj)

    if isinstance(obj, datetime):
        return int(obj.timestamp() * 1000)

    if isinstance(obj, Enum):
        raw = obj.value
        if isinstance(raw, str | int | float | bool) or raw is None:
            return raw
        return str(raw)

    if is_dataclass(obj) and not isinstance(obj, type):
        return _mapping_to_evidence(asdict(obj), depth=depth + 1)

    if isinstance(obj, Mapping):
        return _mapping_to_evidence(obj, depth=depth + 1)

    if isinstance(obj, Sequence) and not isinstance(obj, str | bytes | bytearray):
        return [_to_evidence_value(item, depth=depth + 1) for item in obj]

    fields = _public_fields(obj)
    if fields:
        return _mapping_to_evidence(fields, depth=depth + 1)

    return repr(obj)


def _mapping_to_evidence(
    mapping: Mapping[object, object], *, depth: int
) -> dict[str, IbkrEvidenceValue]:
    out: dict[str, IbkrEvidenceValue] = {}
    for key, value in mapping.items():
        out[str(key)] = _to_evidence_value(value, depth=depth + 1)
    return out


def _public_fields(obj: object) -> dict[str, object]:
    if isinstance(obj, SimpleNamespace):
        return vars(obj)

    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }

    out: dict[str, object] = {}
    for key in dir(obj):
        if key.startswith("_"):
            continue
        try:
            value = getattr(obj, key)
        except Exception:
            continue
        if callable(value):
            continue
        out[key] = value
    return out
