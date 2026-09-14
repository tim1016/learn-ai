"""Evidence-gated retirement for retained unscoped compatibility reads.

Delivery D records only a compact, lane-local aggregate.  Delivery E may
retire an alias only after the host ceremony has paired bounded snapshots with
the complete consumer inventory, scoped-route health evidence, and an explicit
operator receipt.  The artifacts deliberately contain no request identity,
account, Clerk ID, URL, query, header, body, credential, or access log.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.utils.atomic_file import atomic_write_bytes
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import now_ms_utc

COMPATIBILITY_SNAPSHOT_SCHEMA_VERSION = 1
COMPATIBILITY_EVIDENCE_SCHEMA_VERSION = 2
COMPATIBILITY_RETIREMENT_SCHEMA_VERSION = 1
COMPATIBILITY_ROUTE_STATE_SCHEMA_VERSION = 1
COMPATIBILITY_RETIREMENT_ROLLOUT_SCHEMA_VERSION = 1
DEFAULT_MAX_EVIDENCE_AGE_MS = 86_400_000
DEFAULT_MAX_WINDOW_DURATION_MS = 604_800_000
RETIRED_COMPATIBILITY_ROUTE_FAMILIES = frozenset(
    {
        "broker_bots",
        "broker_configuration",
        "broker_v2_panel",
        "brokers_lane_extras",
        "run_replay",
    }
)
RETIRED_COMPATIBILITY_RESPONSE_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})


class CompatibilityRouteState(StrEnum):
    """The only two states for a retained unscoped read alias."""

    MEASUREMENT = "measurement"
    RETIRED = "retired"


class CompatibilityRetirementRolloutState(StrEnum):
    """Host-journal states for a resumable multi-lane retirement."""

    APPLYING = "applying"
    COMPLETE = "complete"


class CompatibilityRetirementRefusal(ValueError):
    """Evidence does not authorize a compatibility-read retirement."""


@dataclass(frozen=True, slots=True)
class CompatibilitySnapshot:
    """One privacy-preserving point-in-time view of one aggregate source."""

    source_label: str
    captured_at_ms: int
    route_hits: tuple[dict[str, int | str], ...]


@dataclass(frozen=True, slots=True)
class CompatibilityRetirementRollout:
    """Durable acknowledgements for one evidence-pinned lane cutover."""

    rollout_id: str
    state: CompatibilityRetirementRolloutState
    route_state_paths: tuple[str, ...]
    retired_route_state_paths: tuple[str, ...]
    retirement_receipt: dict[str, object]
    updated_at_ms: int


def capture_snapshot(
    *, evidence_path: Path, snapshot_path: Path, source_label: str, captured_at_ms: int | None = None
) -> dict[str, object]:
    """Copy one validated aggregate into a durable, labeled snapshot."""
    _require_label(source_label)
    aggregate = _read_json(evidence_path, "compatibility route-hit aggregate")
    route_hits = _validate_route_hits(aggregate, schema_version=COMPATIBILITY_EVIDENCE_SCHEMA_VERSION)
    captured = now_ms_utc() if captured_at_ms is None else captured_at_ms
    _require_timestamp(captured, "snapshot captured_at_ms")
    snapshot = {
        "schema_version": COMPATIBILITY_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": f"compat_{uuid.uuid4().hex}",
        "source_label": source_label,
        "captured_at_ms": captured,
        "route_hits": route_hits,
    }
    _write_json_durable(snapshot_path, snapshot)
    return snapshot


def evaluate_retirement(
    *,
    start_snapshot_paths: Sequence[Path],
    end_snapshot_paths: Sequence[Path],
    consumer_inventory_path: Path,
    scoped_route_evidence_path: Path,
    operator_receipt_path: Path,
    max_evidence_age_ms: int = DEFAULT_MAX_EVIDENCE_AGE_MS,
    max_window_duration_ms: int = DEFAULT_MAX_WINDOW_DURATION_MS,
    evaluated_at_ms: int | None = None,
) -> dict[str, object]:
    """Return the durable evidence receipt that can authorize retirement.

    The caller chooses whether to apply this receipt as ``retired``.  A
    malformed, missing, stale, or internally inconsistent item raises a typed
    refusal; callers must leave aliases in measurement mode.
    """
    if max_evidence_age_ms < 1 or max_window_duration_ms < 1:
        raise CompatibilityRetirementRefusal("Evidence age and window limits must be positive.")
    evaluated = now_ms_utc() if evaluated_at_ms is None else evaluated_at_ms
    _require_timestamp(evaluated, "retirement evaluated_at_ms")
    starts = _read_snapshots(start_snapshot_paths, "start")
    ends = _read_snapshots(end_snapshot_paths, "end")
    if set(starts) != set(ends):
        raise CompatibilityRetirementRefusal(
            "Start and end snapshots must name exactly the same aggregate sources."
        )

    window_start_ms = max(snapshot.captured_at_ms for snapshot in starts.values())
    window_end_ms = min(snapshot.captured_at_ms for snapshot in ends.values())
    if window_start_ms >= window_end_ms:
        raise CompatibilityRetirementRefusal("Measurement snapshots do not form a positive window.")
    if window_end_ms - window_start_ms > max_window_duration_ms:
        raise CompatibilityRetirementRefusal("Measurement window exceeds the configured bounded duration.")
    if evaluated - window_end_ms > max_evidence_age_ms:
        raise CompatibilityRetirementRefusal("End snapshots are stale; collect a fresh representative window.")
    if any(snapshot.captured_at_ms > evaluated for snapshot in (*starts.values(), *ends.values())):
        raise CompatibilityRetirementRefusal("Snapshots cannot be captured after the retirement evaluation.")

    inventory = _read_json(consumer_inventory_path, "consumer inventory")
    consumer_names = _validate_consumer_inventory(inventory, evaluated, max_evidence_age_ms)
    scoped = _read_json(scoped_route_evidence_path, "scoped-route evidence")
    scoped_observed_at_ms = _validate_scoped_route_evidence(scoped)
    _require_fresh(scoped_observed_at_ms, evaluated, max_evidence_age_ms, "Scoped-route evidence")
    receipt = _read_json(operator_receipt_path, "operator receipt")
    receipt_id, receipt_issued_at_ms = _validate_operator_receipt(receipt)
    _require_fresh(receipt_issued_at_ms, evaluated, max_evidence_age_ms, "Operator receipt")
    if receipt_issued_at_ms < window_end_ms:
        raise CompatibilityRetirementRefusal(
            "Operator receipt predates the completed measurement window."
        )

    deltas = _route_deltas(starts, ends)
    if any(delta["count"] != 0 for delta in deltas):
        raise CompatibilityRetirementRefusal(
            "Compatibility route traffic changed during the measurement window."
        )
    return {
        "schema_version": COMPATIBILITY_RETIREMENT_SCHEMA_VERSION,
        "decision": "eligible",
        "evaluated_at_ms": evaluated,
        "measurement_window": {
            "start_ms": window_start_ms,
            "end_ms": window_end_ms,
            "source_labels": sorted(starts),
        },
        "route_deltas": deltas,
        "consumer_inventory": {
            "consumers": sorted(consumer_names),
            "attested_route_families": sorted(RETIRED_COMPATIBILITY_ROUTE_FAMILIES),
        },
        "scoped_route_evidence": {"unresolved_scoped_route_failures": 0},
        "operator_receipt_id": receipt_id,
    }


def write_route_state(
    *, state_path: Path, state: CompatibilityRouteState, retirement_receipt: Mapping[str, object]
) -> dict[str, object]:
    """Persist the local runtime state, retaining only retirement evidence."""
    if state is CompatibilityRouteState.RETIRED:
        _validate_retirement_receipt(retirement_receipt)
    payload: dict[str, object] = {
        "schema_version": COMPATIBILITY_ROUTE_STATE_SCHEMA_VERSION,
        "state": state.value,
        "updated_at_ms": now_ms_utc(),
    }
    if state is CompatibilityRouteState.RETIRED:
        payload["retirement_receipt"] = dict(retirement_receipt)
    _write_json_durable(state_path, payload)
    return payload


def write_retirement_receipt(receipt_path: Path, receipt: Mapping[str, object]) -> None:
    """Durably retain an eligible retirement decision in the rollout record."""
    _validate_retirement_receipt(receipt)
    _write_json_durable(receipt_path, receipt)


def read_retirement_receipt(receipt_path: Path) -> dict[str, object]:
    """Read and validate the durable decision used by a resumed rollout."""
    receipt = _read_json(receipt_path, "compatibility retirement receipt")
    _validate_retirement_receipt(receipt)
    return receipt


def read_retirement_rollout(
    rollout_path: Path,
) -> CompatibilityRetirementRollout | None:
    """Read a resumable host rollout journal, or ``None`` before it starts."""
    if not rollout_path.exists():
        return None
    payload = _read_json(rollout_path, "compatibility retirement rollout")
    expected = {
        "schema_version",
        "rollout_id",
        "state",
        "route_state_paths",
        "retired_route_state_paths",
        "retirement_receipt",
        "updated_at_ms",
    }
    if set(payload) != expected or payload.get("schema_version") != (
        COMPATIBILITY_RETIREMENT_ROLLOUT_SCHEMA_VERSION
    ):
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement rollout has an unknown or incomplete shape."
        )
    rollout_id = payload["rollout_id"]
    if not isinstance(rollout_id, str) or not rollout_id.startswith("compatret_"):
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement rollout has an invalid identity."
        )
    try:
        state = CompatibilityRetirementRolloutState(payload["state"])
    except (TypeError, ValueError) as exc:
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement rollout has an invalid state."
        ) from exc
    route_state_paths = _validate_rollout_paths(
        payload["route_state_paths"], label="route state"
    )
    retired_paths = _validate_rollout_paths(
        payload["retired_route_state_paths"],
        label="retired route state",
        allow_empty=True,
    )
    if not set(retired_paths).issubset(route_state_paths):
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement rollout acknowledges an unknown lane."
        )
    if state is CompatibilityRetirementRolloutState.COMPLETE and set(
        retired_paths
    ) != set(route_state_paths):
        raise CompatibilityRetirementRefusal(
            "A complete compatibility retirement rollout lacks a lane acknowledgement."
        )
    receipt = payload["retirement_receipt"]
    if not isinstance(receipt, Mapping):
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement rollout lacks its decision receipt."
        )
    _validate_retirement_receipt(receipt)
    _require_timestamp(payload["updated_at_ms"], "retirement rollout updated_at_ms")
    return CompatibilityRetirementRollout(
        rollout_id=rollout_id,
        state=state,
        route_state_paths=route_state_paths,
        retired_route_state_paths=retired_paths,
        retirement_receipt=dict(receipt),
        updated_at_ms=payload["updated_at_ms"],
    )


def apply_retirement_rollout(
    *,
    rollout_path: Path,
    state_paths: Sequence[Path],
    retirement_receipt: Mapping[str, object],
) -> CompatibilityRetirementRollout:
    """Retire every lane with a durable, idempotently resumable host journal."""
    _validate_retirement_receipt(retirement_receipt)
    normalized_paths = tuple(str(path.resolve()) for path in state_paths)
    _validate_rollout_paths(list(normalized_paths), label="route state")
    existing = read_retirement_rollout(rollout_path)
    if existing is None:
        rollout = CompatibilityRetirementRollout(
            rollout_id=f"compatret_{uuid.uuid4().hex}",
            state=CompatibilityRetirementRolloutState.APPLYING,
            route_state_paths=normalized_paths,
            retired_route_state_paths=(),
            retirement_receipt=dict(retirement_receipt),
            updated_at_ms=now_ms_utc(),
        )
        _write_retirement_rollout(rollout_path, rollout)
    else:
        if existing.route_state_paths != normalized_paths:
            raise CompatibilityRetirementRefusal(
                "A resumed compatibility retirement must name the original lane paths in order."
            )
        if existing.retirement_receipt != dict(retirement_receipt):
            raise CompatibilityRetirementRefusal(
                "A resumed compatibility retirement must use its original decision receipt."
            )
        rollout = existing

    acknowledged = set(rollout.retired_route_state_paths)
    for state_path_text in rollout.route_state_paths:
        write_route_state(
            state_path=Path(state_path_text),
            state=CompatibilityRouteState.RETIRED,
            retirement_receipt=rollout.retirement_receipt,
        )
        acknowledged.add(state_path_text)
        rollout = CompatibilityRetirementRollout(
            rollout_id=rollout.rollout_id,
            state=CompatibilityRetirementRolloutState.APPLYING,
            route_state_paths=rollout.route_state_paths,
            retired_route_state_paths=tuple(
                path for path in rollout.route_state_paths if path in acknowledged
            ),
            retirement_receipt=rollout.retirement_receipt,
            updated_at_ms=now_ms_utc(),
        )
        _write_retirement_rollout(rollout_path, rollout)

    completed = CompatibilityRetirementRollout(
        rollout_id=rollout.rollout_id,
        state=CompatibilityRetirementRolloutState.COMPLETE,
        route_state_paths=rollout.route_state_paths,
        retired_route_state_paths=rollout.route_state_paths,
        retirement_receipt=rollout.retirement_receipt,
        updated_at_ms=now_ms_utc(),
    )
    _write_retirement_rollout(rollout_path, completed)
    return completed


def read_route_state(state_path: Path) -> CompatibilityRouteState:
    """Read a local state file; absence means the safe default measurement mode."""
    if not state_path.exists():
        return CompatibilityRouteState.MEASUREMENT
    payload = _read_json(state_path, "compatibility route state")
    if payload.get("schema_version") != COMPATIBILITY_ROUTE_STATE_SCHEMA_VERSION:
        raise CompatibilityRetirementRefusal("Compatibility route state has an unsupported schema.")
    try:
        state = CompatibilityRouteState(payload["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CompatibilityRetirementRefusal("Compatibility route state is invalid.") from exc
    _require_timestamp(payload.get("updated_at_ms"), "route state updated_at_ms")
    if state is CompatibilityRouteState.RETIRED:
        receipt = payload.get("retirement_receipt")
        if not isinstance(receipt, Mapping):
            raise CompatibilityRetirementRefusal("Retired compatibility state lacks its evidence receipt.")
        _validate_retirement_receipt(receipt)
    return state


def _read_snapshots(paths: Sequence[Path], boundary: str) -> dict[str, CompatibilitySnapshot]:
    if not paths:
        raise CompatibilityRetirementRefusal(f"At least one {boundary} snapshot is required.")
    snapshots: dict[str, CompatibilitySnapshot] = {}
    for path in paths:
        payload = _read_json(path, f"{boundary} snapshot")
        if payload.get("schema_version") != COMPATIBILITY_SNAPSHOT_SCHEMA_VERSION:
            raise CompatibilityRetirementRefusal(f"{boundary.title()} snapshot has an unsupported schema.")
        label = payload.get("source_label")
        _require_label(label)
        captured_at_ms = payload.get("captured_at_ms")
        _require_timestamp(captured_at_ms, f"{boundary} snapshot captured_at_ms")
        route_hits = _validate_route_hits(payload, schema_version=COMPATIBILITY_SNAPSHOT_SCHEMA_VERSION)
        if label in snapshots:
            raise CompatibilityRetirementRefusal(
                f"{boundary.title()} snapshots duplicate source {label!r}."
            )
        snapshots[label] = CompatibilitySnapshot(label, captured_at_ms, tuple(route_hits))
    return snapshots


def _validate_route_hits(payload: Mapping[str, object], *, schema_version: int) -> list[dict[str, int | str]]:
    if payload.get("schema_version") != schema_version or not isinstance(payload.get("route_hits"), list):
        raise CompatibilityRetirementRefusal("Route-hit evidence is malformed.")
    normalized: list[dict[str, int | str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in payload["route_hits"]:
        if not isinstance(entry, Mapping):
            raise CompatibilityRetirementRefusal("Route-hit evidence contains an invalid entry.")
        family = entry.get("route_family")
        response_class = entry.get("response_class")
        count = entry.get("count")
        first_observed = entry.get("first_observed_at_ms")
        last_observed = entry.get("last_observed_at_ms")
        if (
            not isinstance(family, str)
            or family not in RETIRED_COMPATIBILITY_ROUTE_FAMILIES
            or not isinstance(response_class, str)
            or response_class not in RETIRED_COMPATIBILITY_RESPONSE_CLASSES
        ):
            raise CompatibilityRetirementRefusal("Route-hit evidence has an invalid aggregate key.")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise CompatibilityRetirementRefusal("Route-hit evidence has an invalid count.")
        _require_timestamp(first_observed, "route-hit first_observed_at_ms")
        _require_timestamp(last_observed, "route-hit last_observed_at_ms")
        if first_observed > last_observed:
            raise CompatibilityRetirementRefusal("Route-hit evidence has inverted observation times.")
        key = (family, response_class)
        if key in seen:
            raise CompatibilityRetirementRefusal("Route-hit evidence duplicates an aggregate key.")
        seen.add(key)
        normalized.append(
            {
                "route_family": family,
                "response_class": response_class,
                "count": count,
                "first_observed_at_ms": first_observed,
                "last_observed_at_ms": last_observed,
            }
        )
    return sorted(normalized, key=lambda entry: (str(entry["route_family"]), str(entry["response_class"])))


def _validate_consumer_inventory(payload: Mapping[str, object], evaluated_at_ms: int, max_age_ms: int) -> set[str]:
    if payload.get("schema_version") != 1 or payload.get("complete") is not True:
        raise CompatibilityRetirementRefusal("Consumer inventory must explicitly attest that it is complete.")
    generated_at_ms = payload.get("generated_at_ms")
    _require_fresh(generated_at_ms, evaluated_at_ms, max_age_ms, "Consumer inventory")
    consumers = payload.get("consumers")
    if not isinstance(consumers, list) or not consumers:
        raise CompatibilityRetirementRefusal("Consumer inventory must name at least one attested consumer.")
    names: set[str] = set()
    attested_families: set[str] = set()
    for consumer in consumers:
        if not isinstance(consumer, Mapping):
            raise CompatibilityRetirementRefusal("Consumer inventory contains an invalid entry.")
        name = consumer.get("consumer")
        attestation = consumer.get("attestation")
        if not isinstance(name, str) or not name.strip() or not isinstance(attestation, str) or not attestation.strip():
            raise CompatibilityRetirementRefusal("Every consumer needs a name and migration attestation.")
        if name in names:
            raise CompatibilityRetirementRefusal("Consumer inventory contains a duplicate consumer name.")
        names.add(name)
        families = consumer.get("route_families")
        if not isinstance(families, list) or not families or not all(
            isinstance(family, str) and family in RETIRED_COMPATIBILITY_ROUTE_FAMILIES
            for family in families
        ):
            raise CompatibilityRetirementRefusal(
                "Every consumer attestation must name retained compatibility route families."
            )
        attested_families.update(families)
    missing_families = RETIRED_COMPATIBILITY_ROUTE_FAMILIES - attested_families
    if missing_families:
        raise CompatibilityRetirementRefusal(
            "Consumer inventory lacks attestations for every retained compatibility route family."
        )
    return names


def _validate_scoped_route_evidence(payload: Mapping[str, object]) -> int:
    if payload.get("schema_version") != 1:
        raise CompatibilityRetirementRefusal("Scoped-route evidence has an unsupported schema.")
    observed_at_ms = payload.get("observed_at_ms")
    _require_timestamp(observed_at_ms, "scoped-route evidence observed_at_ms")
    failures = payload.get("unresolved_scoped_route_failures")
    if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
        raise CompatibilityRetirementRefusal("Scoped-route evidence has an invalid unresolved-failure count.")
    if failures != 0:
        raise CompatibilityRetirementRefusal("Scoped-route evidence reports unresolved failures.")
    return observed_at_ms


def _validate_operator_receipt(payload: Mapping[str, object]) -> tuple[str, int]:
    if payload.get("schema_version") != 1 or payload.get("retire_compatibility_reads") is not True:
        raise CompatibilityRetirementRefusal("Operator receipt does not explicitly authorize compatibility retirement.")
    if payload.get("representative_window") is not True:
        raise CompatibilityRetirementRefusal("Operator receipt does not attest that the window is representative.")
    receipt_id = payload.get("receipt_id")
    operator = payload.get("operator")
    issued_at_ms = payload.get("issued_at_ms")
    if not isinstance(receipt_id, str) or not receipt_id.strip() or not isinstance(operator, str) or not operator.strip():
        raise CompatibilityRetirementRefusal("Operator receipt needs a receipt ID and named operator.")
    _require_timestamp(issued_at_ms, "operator receipt issued_at_ms")
    return receipt_id, issued_at_ms


def _route_deltas(
    starts: Mapping[str, CompatibilitySnapshot], ends: Mapping[str, CompatibilitySnapshot]
) -> list[dict[str, int | str]]:
    deltas: dict[tuple[str, str], int] = {}
    for label, start in starts.items():
        before = {(str(entry["route_family"]), str(entry["response_class"])): int(entry["count"]) for entry in start.route_hits}
        after = {(str(entry["route_family"]), str(entry["response_class"])): int(entry["count"]) for entry in ends[label].route_hits}
        keys = set(before) | set(after)
        keys.update(
            (family, response_class)
            for family in RETIRED_COMPATIBILITY_ROUTE_FAMILIES
            for response_class in RETIRED_COMPATIBILITY_RESPONSE_CLASSES
        )
        for key in keys:
            delta = after.get(key, 0) - before.get(key, 0)
            if delta < 0:
                raise CompatibilityRetirementRefusal(
                    "Route-hit aggregate decreased between snapshots; evidence is inconsistent."
                )
            deltas[key] = deltas.get(key, 0) + delta
    return [
        {"route_family": family, "response_class": response_class, "count": count}
        for (family, response_class), count in sorted(deltas.items())
    ]


def _validate_retirement_receipt(receipt: Mapping[str, object]) -> None:
    if receipt.get("schema_version") != COMPATIBILITY_RETIREMENT_SCHEMA_VERSION or receipt.get("decision") != "eligible":
        raise CompatibilityRetirementRefusal("Compatibility retirement state needs an eligible evidence receipt.")
    window = receipt.get("measurement_window")
    if not isinstance(window, Mapping):
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt lacks its measurement window.")
    _require_timestamp(window.get("start_ms"), "retirement receipt window start_ms")
    _require_timestamp(window.get("end_ms"), "retirement receipt window end_ms")
    if window["start_ms"] >= window["end_ms"]:
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt has an invalid window.")
    if not isinstance(receipt.get("operator_receipt_id"), str) or not receipt["operator_receipt_id"].strip():
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt lacks an operator receipt ID.")
    scoped = receipt.get("scoped_route_evidence")
    if not isinstance(scoped, Mapping) or scoped.get("unresolved_scoped_route_failures") != 0:
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt lacks clean scoped-route evidence.")
    inventory = receipt.get("consumer_inventory")
    if not isinstance(inventory, Mapping) or not isinstance(inventory.get("consumers"), list):
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt lacks consumer attestations.")
    if not all(isinstance(consumer, str) and consumer.strip() for consumer in inventory["consumers"]):
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt has invalid consumer attestations.")
    families = inventory.get("attested_route_families")
    if (
        not isinstance(families, list)
        or not all(isinstance(family, str) for family in families)
        or set(families) != RETIRED_COMPATIBILITY_ROUTE_FAMILIES
    ):
        raise CompatibilityRetirementRefusal(
            "Compatibility retirement receipt lacks attestations for retained route families."
        )
    deltas = receipt.get("route_deltas")
    if not isinstance(deltas, list) or any(
        not isinstance(delta, Mapping) or delta.get("count") != 0 for delta in deltas
    ):
        raise CompatibilityRetirementRefusal("Compatibility retirement receipt lacks zero route deltas.")


def _require_label(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 80 or not value.replace("-", "").replace("_", "").isalnum():
        raise CompatibilityRetirementRefusal("Snapshot source labels must be short nonsecret names.")


def _require_timestamp(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > MAX_TIMESTAMP_MS:
        raise CompatibilityRetirementRefusal(f"{label} must be an int64 ms UTC timestamp.")


def _require_fresh(value: object, evaluated_at_ms: int, max_age_ms: int, label: str) -> None:
    _require_timestamp(value, f"{label} timestamp")
    if value > evaluated_at_ms or evaluated_at_ms - value > max_age_ms:
        raise CompatibilityRetirementRefusal(f"{label} is stale or from the future.")


def _validate_rollout_paths(
    raw_paths: object,
    *,
    label: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Validate the closed path inventory stored in a restricted host journal."""
    if (
        not isinstance(raw_paths, list)
        or (not raw_paths and not allow_empty)
        or any(
            not isinstance(path, str) or not path or len(path) > 4096
            for path in raw_paths
        )
        or len(set(raw_paths)) != len(raw_paths)
    ):
        raise CompatibilityRetirementRefusal(
            f"Compatibility retirement {label} paths are invalid or repeated."
        )
    return tuple(raw_paths)


def _read_json(path: Path, artifact: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityRetirementRefusal(f"{artifact.capitalize()} is missing or unreadable.") from exc
    if not isinstance(payload, dict):
        raise CompatibilityRetirementRefusal(f"{artifact.capitalize()} must be a JSON object.")
    return payload


def _write_json_durable(path: Path, payload: Mapping[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, encoded)


def _write_retirement_rollout(
    rollout_path: Path,
    rollout: CompatibilityRetirementRollout,
) -> None:
    """Publish one complete acknowledgement checkpoint."""
    _write_json_durable(
        rollout_path,
        {
            "schema_version": COMPATIBILITY_RETIREMENT_ROLLOUT_SCHEMA_VERSION,
            "rollout_id": rollout.rollout_id,
            "state": rollout.state.value,
            "route_state_paths": list(rollout.route_state_paths),
            "retired_route_state_paths": list(rollout.retired_route_state_paths),
            "retirement_receipt": rollout.retirement_receipt,
            "updated_at_ms": rollout.updated_at_ms,
        },
    )


__all__ = [
    "COMPATIBILITY_RETIREMENT_ROLLOUT_SCHEMA_VERSION",
    "COMPATIBILITY_ROUTE_STATE_SCHEMA_VERSION",
    "RETIRED_COMPATIBILITY_RESPONSE_CLASSES",
    "RETIRED_COMPATIBILITY_ROUTE_FAMILIES",
    "CompatibilityRetirementRefusal",
    "CompatibilityRetirementRollout",
    "CompatibilityRetirementRolloutState",
    "CompatibilityRouteState",
    "apply_retirement_rollout",
    "capture_snapshot",
    "evaluate_retirement",
    "read_retirement_receipt",
    "read_retirement_rollout",
    "read_route_state",
    "write_retirement_receipt",
    "write_route_state",
]
