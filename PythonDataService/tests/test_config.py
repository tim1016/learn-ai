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
