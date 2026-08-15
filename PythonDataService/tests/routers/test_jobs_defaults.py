"""Default-preservation regression tests for jobs.py request models.

The TickerRequest base sets ``multiplier=1`` and ``timespan="minute"``
as defaults. Two of the four jobs models override ``multiplier=15``
to preserve their pre-migration default (without the override the
inheritance would silently switch every caller to 1-minute bars).
These tests pin the post-migration default values so any future change
to the base or to the override surfaces explicitly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.routers.jobs import (
    CrossSectionalJobRequest,
    FeatureResearchJobRequest,
    RuleBasedBacktestJobRequest,
    SignalEngineJobRequest,
    SpyEmaWalkForwardJobRequest,
    start_spy_ema_walk_forward_job,
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


class TestSpyEmaWalkForwardJob:
    def test_request_accepts_only_job_identity(self) -> None:
        from pydantic import ValidationError

        request = SpyEmaWalkForwardJobRequest.model_validate({"jobId": "j1"})
        assert request.job_id == "j1"
        with pytest.raises(ValidationError):
            SpyEmaWalkForwardJobRequest.model_validate({"jobId": "j1", "thresholdsBps": [1, 2]})

    @pytest.mark.asyncio
    async def test_job_runs_the_frozen_v1_protocol(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import app.routers.jobs as jobs_router

        captured: dict[str, Any] = {}

        class _Dump:
            def __init__(self, value: dict[str, Any]) -> None:
                self.value = value

            def model_dump(self, *, mode: str) -> dict[str, Any]:
                assert mode == "json"
                return self.value

        def fake_pipeline(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            kwargs["on_progress"](1, 2, "control persisted")
            kwargs["on_progress"](2, 2, "fold complete")
            return SimpleNamespace(
                control_ledger=_Dump({"run_id": "control"}),
                control_result=_Dump({"metrics": {}}),
                walk_forward_config=_Dump({"protocol_id": "spy-ema-normalized-gap", "protocol_version": "1.0"}),
                walk_forward_result=_Dump({"status": "completed"}),
            )

        class _Cancel:
            def raise_if_cancelled(self) -> None:
                return None

        class _Emitter:
            def __init__(self) -> None:
                self.phases: list[str] = []

            def phase(self, phase: str) -> None:
                self.phases.append(phase)

            def log(self, _message: str) -> None:
                return None

            def progress(self, **_kwargs: Any) -> None:
                return None

        def run_sync(_job_id: str, work, **kwargs):
            captured["thread_kwargs"] = kwargs
            emitter = _Emitter()
            captured["result"] = work(emitter, _Cancel())
            captured["phases"] = emitter.phases

        monkeypatch.setattr(jobs_router, "run_spy_ema_pipeline", fake_pipeline)
        monkeypatch.setattr(jobs_router, "run_in_thread", run_sync)

        response = await start_spy_ema_walk_forward_job(
            SpyEmaWalkForwardJobRequest(job_id="job-1"),
            data_source_factory=lambda *_args: None,
            artifacts_root=tmp_path,
        )

        assert response == {"job_id": "job-1", "status": "queued"}
        request = captured["request"]
        assert request.start_date == "2024-08-01"
        assert request.end_date == "2026-08-01"
        assert request.protocol_id == "spy-ema-normalized-gap"
        assert request.protocol_version == "1.0"
        assert request.thresholds_bps == (1.0, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0)
        assert request.split_policy.describe() == {
            "kind": "rolling",
            "train_days": 180,
            "test_days": 30,
            "step_days": 30,
        }
        assert captured["thread_kwargs"]["cancel_check_every_n"] == 1
        assert captured["phases"] == [
            "running_control",
            "walking_forward",
            "persisting_evidence",
        ]
        assert captured["result"]["walk_forward"]["config"]["protocol_version"] == "1.0"


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
