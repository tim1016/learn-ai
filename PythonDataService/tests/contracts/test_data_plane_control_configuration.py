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


def test_macos_bootstrap_repairs_control_secret_before_compose_startup() -> None:
    bootstrap = (REPOSITORY_ROOT / "setup-macos.sh").read_text(encoding="utf-8")

    copy_environment = bootstrap.index(
        'copy_env_if_missing "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"'
    )
    ensure_secret = bootstrap.index(
        'node "$ROOT_DIR/Frontend/scripts/data-plane-control-secret.cjs" '
        '"$ROOT_DIR/.env"'
    )
    compose_build = bootstrap.index("podman compose build")

    assert copy_environment < ensure_secret < compose_build
