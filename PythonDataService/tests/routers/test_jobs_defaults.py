"""Default-preservation regression tests for jobs.py request models.

The TickerRequest base sets ``multiplier=1`` and ``timespan="minute"``
as defaults. ``SignalEngineJobRequest`` overrides ``multiplier=15``
to preserve its pre-migration default (without the override the
inheritance would silently switch every caller to 1-minute bars).
These tests pin the post-migration default values so any future change
to the base or to the override surfaces explicitly.
"""

from __future__ import annotations

from app.routers.jobs import (
    CrossSectionalJobRequest,
    SignalEngineJobRequest,
)


class TestSignalEngineDefaults:
    def test_pre_migration_defaults_preserved(self) -> None:
        s = SignalEngineJobRequest(
            job_id="t",
            symbol="SPY",
            feature_name="rsi",
            from_date="2025-01-01",
            to_date="2025-01-31",
        )
        assert s.multiplier == 15, "multiplier defaults to 15 pre-migration, NOT 1"
        assert s.flip_sign is True, "flip_sign default is preserved"
        assert s.regime_gate_enabled is True, "regime_gate_enabled default is preserved"


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
        r = SignalEngineJobRequest.model_validate(
            {
                "jobId": "j1",
                "symbol": "SPY",
                "featureName": "rsi",
                "fromDate": "2025-01-01",
                "toDate": "2025-01-31",
            }
        )
        assert r.symbol == "SPY"
        assert r.from_date == "2025-01-01"
        assert r.to_date == "2025-01-31"

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
