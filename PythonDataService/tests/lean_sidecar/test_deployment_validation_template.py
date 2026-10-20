from __future__ import annotations

import ast

from app.lean_sidecar.trusted_samples.deployment_validation import (
    DEPLOYMENT_VALIDATION_SOURCE,
)


def test_source_matches_the_deployment_validation_template_spec() -> None:
    src = DEPLOYMENT_VALIDATION_SOURCE
    tree = ast.parse(src)
    method_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert "Initialize" in method_names, "the template defines the Initialize handler"
    assert "OnData" in method_names, "the template defines the OnData handler"
    assert "OnEndOfAlgorithm" in method_names, "the template defines the OnEndOfAlgorithm handler"

    assert "DETECTION_START_OFFSET = timedelta(minutes=15)" in src, "detection starts 15 minutes in"
    assert "STOP_AND_FLATTEN_OFFSET = timedelta(minutes=15)" in src, "stop and flatten offset is 15 minutes"
    assert "EXIT_BAR_COUNT = 3" in src, "the exit bar count is 3"

    assert "green = bar.Close > bar.Open" in src, "a green bar is a close above the open"
    assert "self._reset_detection()" in src, "the green-bar detection is reset"
    assert "self.entry_pending = True" in src, "an entry is marked pending"

    assert "observations.csv" in src, "the template writes observations.csv"
    assert "state.csv" in src, "the template writes state.csv"
    assert "ms_utc,open,high,low,close,volume" in src, "the observations.csv header is pinned"
    assert "ts_ms_utc,open,close,green_streak,signal" in src, "the state.csv header is pinned"

    assert "SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage, AccountType.Margin)" in src, (
        "the template pins the Interactive Brokers margin brokerage"
    )
