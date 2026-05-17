"""LeanConfig payload tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.lean_sidecar.lean_config import (
    CONTAINER_DATA_FOLDER,
    CONTAINER_PYTHON_ALGORITHM,
    CONTAINER_RESULTS_FOLDER,
    LeanConfig,
)


class TestLeanConfigPayload:
    def test_python_defaults_point_at_container_paths(self) -> None:
        payload = LeanConfig().to_payload()
        assert payload["algorithm-language"] == "Python"
        assert payload["algorithm-type-name"] == "MyAlgorithm"
        assert payload["algorithm-location"] == CONTAINER_PYTHON_ALGORITHM
        assert payload["data-folder"] == CONTAINER_DATA_FOLDER
        assert payload["results-destination-folder"] == CONTAINER_RESULTS_FOLDER

    def test_parameters_passed_through(self) -> None:
        config = LeanConfig(parameters={"start_date": "2025-01-06"})
        payload = config.to_payload()
        assert payload["parameters"] == {"start_date": "2025-01-06"}

    def test_write_produces_sorted_pretty_json(self, tmp_path: Path) -> None:
        path = LeanConfig().write(tmp_path / "config.json")
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert list(parsed.keys()) == sorted(parsed.keys())
        # Pretty-printing produces an indented body.
        assert "\n  " in raw

    def test_csharp_payload_points_at_assembly(self) -> None:
        payload = LeanConfig(algorithm_language="CSharp").to_payload()
        assert payload["algorithm-language"] == "CSharp"
        assert payload["algorithm-location"].endswith(".dll")
