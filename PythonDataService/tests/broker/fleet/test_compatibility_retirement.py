"""Delivery-E compatibility-retirement evidence and alias-state tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.fleet.compatibility_retirement import (
    CompatibilityRetirementRefusal,
    CompatibilityRouteState,
    capture_snapshot,
    evaluate_retirement,
    read_route_state,
    write_route_state,
)


def _aggregate(count: int, timestamp_ms: int) -> dict[str, object]:
    route_hits: list[dict[str, object]] = []
    if count:
        route_hits.append(
            {
                "route_family": "broker_bots",
                "response_class": "2xx",
                "count": count,
                "first_observed_at_ms": timestamp_ms,
                "last_observed_at_ms": timestamp_ms,
            }
        )
    return {"schema_version": 2, "updated_at_ms": timestamp_ms, "route_hits": route_hits}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _complete_evidence(tmp_path: Path) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    start_aggregate = tmp_path / "start-aggregate.json"
    end_aggregate = tmp_path / "end-aggregate.json"
    start_snapshot = tmp_path / "start-snapshot.json"
    end_snapshot = tmp_path / "end-snapshot.json"
    _write_json(start_aggregate, _aggregate(2, 1_000))
    _write_json(end_aggregate, _aggregate(2, 2_000))
    capture_snapshot(
        evidence_path=start_aggregate,
        snapshot_path=start_snapshot,
        source_label="paper",
        captured_at_ms=1_000,
    )
    capture_snapshot(
        evidence_path=end_aggregate,
        snapshot_path=end_snapshot,
        source_label="paper",
        captured_at_ms=2_000,
    )
    inventory = tmp_path / "consumers.json"
    scoped = tmp_path / "scoped.json"
    operator = tmp_path / "operator.json"
    _write_json(
        inventory,
        {
            "schema_version": 1,
            "complete": True,
            "generated_at_ms": 2_000,
            "consumers": [
                {
                    "consumer": "alpaca-desk",
                    "attestation": "scoped-route-migrated",
                    "route_families": [
                        "broker_bots",
                        "broker_configuration",
                        "broker_v2_panel",
                        "brokers_lane_extras",
                        "run_replay",
                    ],
                }
            ],
        },
    )
    _write_json(
        scoped,
        {"schema_version": 1, "observed_at_ms": 2_000, "unresolved_scoped_route_failures": 0},
    )
    _write_json(
        operator,
        {
            "schema_version": 1,
            "receipt_id": "operator-acceptance-1",
            "operator": "fleet-owner",
            "issued_at_ms": 2_100,
            "representative_window": True,
            "retire_compatibility_reads": True,
        },
    )
    return {
        "start": start_snapshot,
        "end": end_snapshot,
        "inventory": inventory,
        "scoped": scoped,
        "operator": operator,
    }


def _evaluate(paths: dict[str, Path]) -> dict[str, object]:
    return evaluate_retirement(
        start_snapshot_paths=[paths["start"]],
        end_snapshot_paths=[paths["end"]],
        consumer_inventory_path=paths["inventory"],
        scoped_route_evidence_path=paths["scoped"],
        operator_receipt_path=paths["operator"],
        evaluated_at_ms=2_200,
        max_evidence_age_ms=10_000,
        max_window_duration_ms=10_000,
    )


def test_evaluate_retirement_requires_all_nontraffic_evidence(tmp_path: Path) -> None:
    """A one-hit delta is not enough: inventory, scoped health, and receipt all gate retirement."""
    receipt = _evaluate(_complete_evidence(tmp_path))

    assert receipt["decision"] == "eligible"
    assert len(receipt["route_deltas"]) == 20
    assert all(delta["count"] == 0 for delta in receipt["route_deltas"])
    serialized = json.dumps(receipt)
    for forbidden in ("account", "clrk_", "token", "secret", "url"):
        assert forbidden not in serialized


@pytest.mark.parametrize("field,value", [("complete", False), ("consumers", [])])
def test_evaluate_retirement_refuses_incomplete_consumer_inventory(
    tmp_path: Path, field: str, value: object
) -> None:
    """Zero aggregate traffic cannot replace named consumer attestations."""
    paths = _complete_evidence(tmp_path)
    inventory = json.loads(paths["inventory"].read_text(encoding="utf-8"))
    inventory[field] = value
    _write_json(paths["inventory"], inventory)

    with pytest.raises(CompatibilityRetirementRefusal, match="Consumer inventory"):
        _evaluate(paths)


def test_evaluate_retirement_requires_attestation_for_every_retained_route_family(
    tmp_path: Path,
) -> None:
    """A named consumer without complete route-family coverage cannot authorize retirement."""
    paths = _complete_evidence(tmp_path)
    inventory = json.loads(paths["inventory"].read_text(encoding="utf-8"))
    inventory["consumers"][0]["route_families"].pop()
    _write_json(paths["inventory"], inventory)

    with pytest.raises(CompatibilityRetirementRefusal, match="every retained"):
        _evaluate(paths)


def test_evaluate_retirement_refuses_traffic_stale_inconsistent_and_scoped_failures(tmp_path: Path) -> None:
    """Retirement closes on compatibility traffic, stale/decreasing data, and scoped failures."""
    paths = _complete_evidence(tmp_path)
    end = json.loads(paths["end"].read_text(encoding="utf-8"))
    end["route_hits"][0]["count"] = 3
    _write_json(paths["end"], end)
    with pytest.raises(CompatibilityRetirementRefusal, match="traffic changed"):
        _evaluate(paths)

    paths = _complete_evidence(tmp_path / "decreased")
    end = json.loads(paths["end"].read_text(encoding="utf-8"))
    end["route_hits"][0]["count"] = 1
    _write_json(paths["end"], end)
    with pytest.raises(CompatibilityRetirementRefusal, match="decreased"):
        _evaluate(paths)

    paths = _complete_evidence(tmp_path / "scoped")
    scoped = json.loads(paths["scoped"].read_text(encoding="utf-8"))
    scoped["unresolved_scoped_route_failures"] = 1
    _write_json(paths["scoped"], scoped)
    with pytest.raises(CompatibilityRetirementRefusal, match="unresolved"):
        _evaluate(paths)

    paths = _complete_evidence(tmp_path / "stale")
    with pytest.raises(CompatibilityRetirementRefusal, match="stale"):
        evaluate_retirement(
            start_snapshot_paths=[paths["start"]],
            end_snapshot_paths=[paths["end"]],
            consumer_inventory_path=paths["inventory"],
            scoped_route_evidence_path=paths["scoped"],
            operator_receipt_path=paths["operator"],
            evaluated_at_ms=20_000,
            max_evidence_age_ms=10,
            max_window_duration_ms=10_000,
        )


def test_route_state_defaults_to_measurement_and_requires_an_eligible_receipt(tmp_path: Path) -> None:
    """Missing state preserves compatibility; a malformed retirement never retires an alias."""
    state_path = tmp_path / "compatibility" / "route_state.json"
    assert read_route_state(state_path) is CompatibilityRouteState.MEASUREMENT
    with pytest.raises(CompatibilityRetirementRefusal, match="eligible"):
        write_route_state(
            state_path=state_path,
            state=CompatibilityRouteState.RETIRED,
            retirement_receipt={"schema_version": 1, "decision": "ineligible"},
        )

    receipt = _evaluate(_complete_evidence(tmp_path / "eligible"))
    write_route_state(
        state_path=state_path,
        state=CompatibilityRouteState.RETIRED,
        retirement_receipt=receipt,
    )
    assert read_route_state(state_path) is CompatibilityRouteState.RETIRED
