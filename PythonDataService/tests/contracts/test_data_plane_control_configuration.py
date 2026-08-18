"""Static configuration contract for the shared data-plane control secret."""

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTROL_SECRET_ENVIRONMENT_KEY = "DATA_PLANE_CONTROL_SECRET"
CONTROL_SECRET_REQUIRED_INTERPOLATION = re.compile(
    r"\$\{DATA_PLANE_CONTROL_SECRET:\?[^}]+\}"
)


def test_compose_requires_one_shared_control_secret_for_service_and_proxy() -> None:
    compose = yaml.safe_load(
        (REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8")
    )

    configured_values: list[str] = []
    for service_name in ("python-service", "frontend"):
        environment = compose["services"][service_name]["environment"]
        assignments = {
            key: value
            for key, separator, value in (entry.partition("=") for entry in environment)
            if separator
        }
        configured_values.append(assignments[CONTROL_SECRET_ENVIRONMENT_KEY])

    assert configured_values[0] == configured_values[1]
    assert CONTROL_SECRET_REQUIRED_INTERPOLATION.fullmatch(configured_values[0])
