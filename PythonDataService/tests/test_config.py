"""Tests for app.config.Settings and related frozen configs."""

from __future__ import annotations

from app.config import Settings
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
