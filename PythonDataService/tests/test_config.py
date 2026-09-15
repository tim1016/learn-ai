"""Tests for app.config.Settings and related frozen configs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import FleetSettings, Settings
from app.research.config import ResearchConfig
from app.research.signal.config import SignalConfig


def test_settings_loads_with_env_key(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "pk-unit-test")
    monkeypatch.setenv("FRED_API_KEY", "")

    settings = Settings()

    assert settings.POLYGON_API_KEY == "pk-unit-test"
    assert settings.HOST == "0.0.0.0"
    assert settings.PORT == 8000


def test_settings_allowed_origins_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "pk-unit-test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://a.test,http://b.test , http://c.test ")

    settings = Settings()

    assert settings.get_allowed_origins() == [
        "http://a.test",
        "http://b.test",
        "http://c.test",
    ]


def test_fleet_worker_service_accepts_a_compose_service_name() -> None:
    assert FleetSettings(WORKER_SERVICE="alpaca-paper-clerk").WORKER_SERVICE == (
        "alpaca-paper-clerk"
    )


def test_fleet_worker_service_reads_an_undeclared_deployment_as_none() -> None:
    """An empty declaration is "this deployment said nothing", not a service
    named "". Compose writes an unset `${VAR}` through as an empty string, so
    the two must collapse to the same absent value."""
    assert FleetSettings(WORKER_SERVICE="").WORKER_SERVICE is None


def test_fleet_worker_service_reads_the_declared_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The constructor-kwarg tests above prove the field and its validator;
    this proves the env var actually reaches the field. A typo in the field
    name, or a change to `env_prefix`, would break every deployment while
    leaving those tests green."""
    monkeypatch.setenv("FLEET_WORKER_SERVICE", "alpaca-paper-clerk")
    assert FleetSettings().WORKER_SERVICE == "alpaca-paper-clerk"


@pytest.mark.parametrize("declared", ["alpaca paper", "a;rm -rf", "Alpaca-Paper", "-leading"])
def test_fleet_worker_service_refuses_a_value_no_compose_service_could_be(declared: str) -> None:
    """The value is pasted verbatim into a command the operator runs. A
    declaration that is not a compose service name is a deployment mistake and
    must stop the process at boot rather than reach a copy button."""
    with pytest.raises(ValidationError):
        FleetSettings(WORKER_SERVICE=declared)


def test_fleet_compose_project_and_profile_accept_compose_tokens() -> None:
    declared = FleetSettings(COMPOSE_PROJECT="learn-ai-fleet", COMPOSE_PROFILE="fleet")

    assert declared.COMPOSE_PROJECT == "learn-ai-fleet"
    assert declared.COMPOSE_PROFILE == "fleet"


def test_fleet_compose_project_and_profile_read_an_undeclared_deployment_as_none() -> None:
    """Same collapse as the worker service: Compose writes an unset `${VAR}`
    through as an empty string, and "the host's defaults resolve this" must not
    reach the command as a `--project-name ''`."""
    declared = FleetSettings(COMPOSE_PROJECT="", COMPOSE_PROFILE="")

    assert declared.COMPOSE_PROJECT is None
    assert declared.COMPOSE_PROFILE is None
    assert FleetSettings().COMPOSE_PROJECT is None
    assert FleetSettings().COMPOSE_PROFILE is None


@pytest.mark.parametrize("declared", ["Learn-AI", "learn ai", "-leading", "learn/ai", "learn.ai"])
def test_fleet_compose_project_refuses_a_value_no_compose_project_could_be(declared: str) -> None:
    with pytest.raises(ValidationError):
        FleetSettings(COMPOSE_PROJECT=declared)


@pytest.mark.parametrize("declared", ["Fleet", "two words", "-leading", "a/b"])
def test_fleet_compose_profile_refuses_a_value_no_compose_profile_could_be(declared: str) -> None:
    with pytest.raises(ValidationError):
        FleetSettings(COMPOSE_PROFILE=declared)


def test_fleet_compose_files_reads_a_comma_separated_declaration_in_order() -> None:
    """Compose resolves `-f` files in the order they are given, so the declared
    order is the command's order."""
    declared = FleetSettings(COMPOSE_FILES=" compose.yaml , compose.fleet.yaml ,, ")

    assert declared.get_compose_files() == ("compose.yaml", "compose.fleet.yaml")


def test_fleet_compose_files_reads_an_undeclared_deployment_as_no_files() -> None:
    assert FleetSettings().get_compose_files() == ()
    assert FleetSettings(COMPOSE_FILES="").get_compose_files() == ()


def test_fleet_compose_context_reads_the_declared_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var is the only path a deployment has, and a comma-separated
    list is not JSON — pydantic-settings decodes a complex-typed field from the
    environment before any validator sees it, so the field stays a plain string
    and the split lives in `get_compose_files`."""
    monkeypatch.setenv("FLEET_COMPOSE_PROJECT", "learn-ai-fleet")
    monkeypatch.setenv("FLEET_COMPOSE_FILES", "compose.yaml,compose.fleet.yaml")
    monkeypatch.setenv("FLEET_COMPOSE_PROFILE", "fleet")

    declared = FleetSettings()

    assert declared.COMPOSE_PROJECT == "learn-ai-fleet"
    assert declared.get_compose_files() == ("compose.yaml", "compose.fleet.yaml")
    assert declared.COMPOSE_PROFILE == "fleet"


@pytest.mark.parametrize(
    "declared",
    [
        "../compose.yaml",
        "deploy/compose.yaml",
        "/compose.yaml",
        "compose file.yaml",
        "Compose.yaml",
        "compose.txt",
        "compose.yaml;rm -rf /",
        "compose.yaml,../secrets.yaml",
    ],
)
def test_fleet_compose_files_refuses_anything_but_a_repo_root_compose_file(declared: str) -> None:
    """Every entry is pasted verbatim after a `-f` in a command an operator
    runs. Bare, lowercase `*.yaml`/`*.yml` names only: no separators and no
    `..`, so nothing outside the repo root can be named and no second word can
    be smuggled into the command."""
    with pytest.raises(ValidationError):
        FleetSettings(COMPOSE_FILES=declared)


def test_research_config_defaults_are_frozen_and_stable():
    cfg = ResearchConfig()

    assert cfg.horizon == 15
    assert cfg.n_bins == 5
    assert cfg.adf_significance == 0.05
    assert cfg.kpss_significance == 0.05
    assert cfg.ic_correlation_method == "spearman"
    assert cfg.monotonicity_threshold == 0.75


def test_research_config_frozen_raises_on_mutation():
    cfg = ResearchConfig()

    try:
        cfg.horizon = 30  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ResearchConfig should be frozen")


def test_signal_config_defaults():
    cfg = SignalConfig()

    assert cfg.feature_name == "momentum_5m"
    assert cfg.horizon == 15
    assert cfg.flip_sign is True
    assert cfg.regime_gate_enabled is True
    assert cfg.walk_forward_train_months == 3
    assert cfg.walk_forward_test_months == 1
    assert cfg.thresholds == (0.5, 1.0, 1.5, 2.0)
