"""Default-preservation regression tests for jobs.py request models.

The TickerRequest base sets ``multiplier=1`` and ``timespan="minute"``
as defaults. Two of the four jobs models override ``multiplier=15``
to preserve their pre-migration default (without the override the
inheritance would silently switch every caller to 1-minute bars).
These tests pin the post-migration default values so any future change
to the base or to the override surfaces explicitly.
"""

from __future__ import annotations

import pytest

from app.routers.jobs import (
    CrossSectionalJobRequest,
    FeatureResearchJobRequest,
    RuleBasedBacktestJobRequest,
    SignalEngineJobRequest,
)


class TestRuleBasedBacktestDefaults:
    def test_multiplier_defaults_to_15_pre_migration(self) -> None:
        r = RuleBasedBacktestJobRequest(
            job_id="t",
            symbol="SPY",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert r.multiplier == 15  # NOT 1 (the base default)

    def test_timespan_defaults_to_minute(self) -> None:
        r = RuleBasedBacktestJobRequest(
            job_id="t",
            symbol="SPY",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert r.timespan == "minute"


class TestSignalEngineDefaults:
    def test_multiplier_defaults_to_15_pre_migration(self) -> None:
        s = SignalEngineJobRequest(
            job_id="t",
            symbol="SPY",
            feature_name="rsi",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert s.multiplier == 15  # NOT 1

    def test_flip_sign_default_preserved(self) -> None:
        s = SignalEngineJobRequest(
            job_id="t",
            symbol="SPY",
            feature_name="rsi",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert s.flip_sign is True
        assert s.regime_gate_enabled is True


class TestFeatureResearchDefaults:
    def test_multiplier_matches_base_default(self) -> None:
        f = FeatureResearchJobRequest(
            job_id="t",
            symbol="SPY",
            feature_name="rsi",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert f.multiplier == 1  # matches base


class TestCrossSectionalDefaults:
    def test_multi_ticker_inheritance(self) -> None:
        c = CrossSectionalJobRequest(
            job_id="t",
            feature_name="rsi",
            symbols=["SPY", "QQQ"],
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert c.symbols == ["SPY", "QQQ"]
        assert c.target_type == "directional"
        assert c.force is False


class TestCamelCaseWire:
    """The .NET JobsApi forwards JSON raw, so jobs models must accept
    camelCase field names on the wire (fromDate, toDate, jobId, etc.)
    in addition to canonical snake_case for in-process construction."""

    def test_camel_case_canonical_names(self) -> None:
        # Post-PR(iii) — Frontend sends canonical 'symbol' (not 'ticker')
        # in camelCase wire format. Verifies the .NET-forwarded shape
        # works after the AliasChoices for legacy snake_case names came
        # off in PR (iii).
        r = RuleBasedBacktestJobRequest.model_validate(
            {
                "jobId": "j1",
                "symbol": "SPY",
                "fromDate": "2025-01-01",
                "toDate": "2025-01-31",
            }
        )
        assert r.symbol == "SPY"
        assert r.from_date == "2025-01-01"
        assert r.to_date == "2025-01-31"

    def test_camel_case_legacy_ticker_no_longer_accepted(self) -> None:
        # PR (iii) removed the legacy 'ticker' alias. With extra="forbid",
        # the unknown field surfaces as a 422.
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            RuleBasedBacktestJobRequest.model_validate(
                {
                    "jobId": "j1",
                    "ticker": "SPY",  # legacy — no longer accepted
                    "fromDate": "2025-01-01",
                    "toDate": "2025-01-31",
                }
            )
        s = str(exc.value).lower()
        assert "ticker" in s and ("extra" in s or "symbol" in s)

    def test_camel_case_canonical_symbols_for_multi(self) -> None:
        c = CrossSectionalJobRequest.model_validate(
            {
                "jobId": "j2",
                "featureName": "rsi",
                "symbols": ["SPY", "QQQ"],
                "fromDate": "2025-01-01",
                "toDate": "2025-01-31",
            }
        )
        assert c.symbols == ["SPY", "QQQ"]

    def test_camel_case_legacy_tickers_no_longer_accepted(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CrossSectionalJobRequest.model_validate(
                {
                    "jobId": "j2",
                    "featureName": "rsi",
                    "tickers": ["SPY", "QQQ"],  # legacy — no longer accepted
                    "fromDate": "2025-01-01",
                    "toDate": "2025-01-31",
                }
            )
