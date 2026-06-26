"""Row-level matching for Activity broker API evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import JsonValue

from app.broker.ibkr.api_evidence import IbkrApiEvidenceEvent
from app.schemas.live_runs import ActivityEvidenceRef

_IDENTITY_KEYS = frozenset(
    {
        "orderRef",
        "order_ref",
        "orderId",
        "order_id",
        "permId",
        "perm_id",
        "execId",
        "exec_id",
        "symbol",
    }
)


def activity_evidence_ref_from_event(event: IbkrApiEvidenceEvent) -> ActivityEvidenceRef:
    """Create an Activity evidence ref with broker identity when observable."""

    identity = _extract_identity(
        {
            "request": event.request.model_dump(mode="json"),
            "response": event.response.model_dump(mode="json") if event.response else None,
        }
    )
    symbol = _as_str_or_none(identity.get("symbol")) or event.symbol
    return ActivityEvidenceRef(
        source=event.source,
        seq=event.seq,
        ts_ms=event.ts_ms,
        request_call=str(event.request.call),
        response_callback=str(event.response.callback) if event.response else None,
        order_ref=_as_str_or_none(identity.get("order_ref")),
        order_id=_as_int_or_none(identity.get("order_id")),
        perm_id=_as_int_or_none(identity.get("perm_id")),
        exec_id=_as_str_or_none(identity.get("exec_id")),
        symbol=symbol,
    )


def matching_evidence_refs(
    row: object,
    refs: Iterable[ActivityEvidenceRef],
    *,
    request_calls: set[str],
) -> list[ActivityEvidenceRef]:
    """Return refs for ``row`` matched by concrete broker identity."""

    row_identity = _row_identity(row)
    if not row_identity:
        return []
    return [
        ref
        for ref in refs
        if ref.request_call in request_calls and _ref_matches_identity(ref, row_identity)
    ]


def _extract_identity(value: JsonValue | Mapping[str, Any] | list[Any] | None) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    _collect_identity(value, identity)
    return identity


def _collect_identity(value: object, identity: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        fields = value.get("fields")
        if isinstance(fields, Mapping):
            _collect_identity(fields, identity)
        for key, item in value.items():
            if key in _IDENTITY_KEYS:
                _assign_identity(identity, key, item)
            elif isinstance(item, (Mapping, list)):
                _collect_identity(item, identity)
    elif isinstance(value, list):
        for item in value:
            _collect_identity(item, identity)


def _assign_identity(identity: dict[str, Any], key: str, value: object) -> None:
    if key in {"orderRef", "order_ref"}:
        identity.setdefault("order_ref", value)
    elif key in {"orderId", "order_id"}:
        identity.setdefault("order_id", value)
    elif key in {"permId", "perm_id"}:
        identity.setdefault("perm_id", value)
    elif key in {"execId", "exec_id"}:
        identity.setdefault("exec_id", value)
    elif key == "symbol":
        identity.setdefault("symbol", value)


def _row_identity(row: object) -> dict[str, str | int]:
    identity: dict[str, str | int] = {}
    order_ref = _as_str_or_none(getattr(row, "order_ref", None))
    if order_ref:
        identity["order_ref"] = order_ref
    exec_id = _as_str_or_none(getattr(row, "exec_id", None))
    if exec_id:
        identity["exec_id"] = exec_id
    perm_id = _as_int_or_none(getattr(row, "perm_id", None))
    if perm_id is not None:
        identity["perm_id"] = perm_id
    order_id = _as_int_or_none(getattr(row, "order_id", None))
    if order_id is not None:
        identity["order_id"] = order_id
    return identity


def _ref_matches_identity(ref: ActivityEvidenceRef, row_identity: dict[str, str | int]) -> bool:
    if ref.exec_id and row_identity.get("exec_id") == ref.exec_id:
        return True
    if ref.order_ref and row_identity.get("order_ref") == ref.order_ref:
        return True
    if ref.perm_id is not None and row_identity.get("perm_id") == ref.perm_id:
        return True
    return ref.order_id is not None and row_identity.get("order_id") == ref.order_id


def _as_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["activity_evidence_ref_from_event", "matching_evidence_refs"]
