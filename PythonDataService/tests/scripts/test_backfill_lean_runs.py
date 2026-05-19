"""Tests for app.scripts.backfill_lean_runs."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.scripts.backfill_lean_runs import (
    _algorithm_name_from_manifest,
    _build_payload_for_workspace,
    backfill_directory,
)

_BACKEND_URL = "http://test-backend"
_PERSIST_URL = f"{_BACKEND_URL}/api/backtest-runs/persist-lean"


def _write_workspace(
    root: Path,
    run_id: str,
    *,
    symbol: str = "SPY",
    start_date: str = "2025-01-06",
    end_date: str = "2025-01-10",
    starting_cash: float = 100_000.0,
    notes: list[str] | None = None,
    include_normalized: bool = True,
    include_manifest: bool = True,
    manifest_overrides: dict | None = None,
) -> Path:
    """Build a minimal but valid LEAN workspace under ``root/run_id/``."""
    workspace = root / run_id
    workspace.mkdir(parents=True)
    (workspace / "normalized").mkdir()
    (workspace / "workspace").mkdir()

    if include_normalized:
        result = {
            "algorithm_id": "MyAlgorithm",
            "parser_version": "phase-3a-r1",
            "first_equity_ms_utc": 1_736_053_200_000,
            "last_equity_ms_utc": 1_736_348_400_000,
            "total_equity_points": 1,
            "total_order_events": 0,
            "equity_curve": [
                {
                    "ms_utc": 1_736_348_400_000,
                    "value": 100_000.0,
                    "open": 100_000.0,
                    "high": 100_000.0,
                    "low": 100_000.0,
                }
            ],
            "order_events": [],
            "statistics": {},
            "runtime_statistics": {},
        }
        (workspace / "normalized" / "result.json").write_text(json.dumps(result))

    if include_manifest:
        manifest = {
            "parameters": {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "starting_cash": starting_cash,
            },
            "effective_algorithm_window_ms": {
                "start_ms": 1_736_053_200_000,
                "end_ms": 1_736_348_400_000,
            },
            "notes": notes if notes is not None else ["trusted_template=ema_crossover"],
        }
        if manifest_overrides:
            manifest.update(manifest_overrides)
        (workspace / "manifest.json").write_text(json.dumps(manifest))

    return workspace


class TestAlgorithmNameFromManifest:
    def test_trusted_template_returns_template_name(self) -> None:
        manifest = {"notes": ["algorithm_source_kind=trusted_sample", "trusted_template=ema_crossover"]}
        assert _algorithm_name_from_manifest(manifest) == "ema_crossover"

    def test_user_source_returns_user_provided(self) -> None:
        manifest = {"notes": ["algorithm_source_kind=user_source", "trusted_template=trusted_default"]}
        # user_source wins over any template hint — matches lean_sidecar_persistence's _algorithm_name_for_run
        assert _algorithm_name_from_manifest(manifest) == "user_provided"

    def test_no_notes_falls_back_to_user_provided(self) -> None:
        assert _algorithm_name_from_manifest({}) == "user_provided"
        assert _algorithm_name_from_manifest({"notes": []}) == "user_provided"

    def test_unrelated_notes_ignored(self) -> None:
        manifest = {"notes": ["is_clean=True", "exit_code=0"]}
        assert _algorithm_name_from_manifest(manifest) == "user_provided"


class TestBuildPayloadForWorkspace:
    def test_well_formed_workspace_builds_payload(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_ok")

        payload = _build_payload_for_workspace(workspace)

        assert payload is not None
        assert payload["lean_run_id"] == "ws_ok"
        assert payload["source"] == "lean-sidecar"
        assert payload["symbol"] == "SPY"
        assert payload["strategy_name"] == "ema_crossover"
        assert payload["starting_cash"] == 100_000.0
        assert payload["start_date_ms"] == 1_736_053_200_000
        assert payload["end_date_ms"] == 1_736_348_400_000

    def test_missing_manifest_returns_none(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_no_manifest", include_manifest=False)

        assert _build_payload_for_workspace(workspace) is None

    def test_missing_normalized_result_returns_none(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_no_result", include_normalized=False)

        assert _build_payload_for_workspace(workspace) is None

    def test_invalid_manifest_json_returns_none(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_bad_manifest")
        (workspace / "manifest.json").write_text("{ this is not json")

        assert _build_payload_for_workspace(workspace) is None

    def test_missing_symbol_in_parameters_returns_none(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_no_symbol")
        # Overwrite manifest to drop symbol
        manifest = json.loads((workspace / "manifest.json").read_text())
        del manifest["parameters"]["symbol"]
        (workspace / "manifest.json").write_text(json.dumps(manifest))

        assert _build_payload_for_workspace(workspace) is None

    def test_missing_window_ms_returns_none(self, tmp_path: Path) -> None:
        workspace = _write_workspace(tmp_path, "ws_no_window")
        manifest = json.loads((workspace / "manifest.json").read_text())
        del manifest["effective_algorithm_window_ms"]
        (workspace / "manifest.json").write_text(json.dumps(manifest))

        assert _build_payload_for_workspace(workspace) is None

    def test_user_source_workspace_persists_as_user_provided(self, tmp_path: Path) -> None:
        workspace = _write_workspace(
            tmp_path,
            "ws_user",
            notes=["algorithm_source_kind=user_source"],
        )

        payload = _build_payload_for_workspace(workspace)

        assert payload is not None
        assert payload["strategy_name"] == "user_provided"


class TestBackfillDirectory:
    @pytest.mark.asyncio
    async def test_backfills_one_workspace_per_directory(self, tmp_path: Path) -> None:
        _write_workspace(tmp_path, "run_a")
        _write_workspace(tmp_path, "run_b")

        # Each persist returns a unique strategy_execution_id
        responses = iter(
            [
                httpx.Response(200, json={"strategy_execution_id": 101}),
                httpx.Response(200, json={"strategy_execution_id": 102}),
            ]
        )

        async with respx.mock(base_url=_BACKEND_URL, assert_all_called=True) as mock:
            mock.post("/api/backtest-runs/persist-lean").mock(side_effect=lambda req: next(responses))

            persisted_ids = await backfill_directory(tmp_path, _BACKEND_URL)

            assert persisted_ids == [101, 102]

    @pytest.mark.asyncio
    async def test_is_idempotent_when_backend_dedupes(self, tmp_path: Path) -> None:
        # The real .NET endpoint returns the same StrategyExecution.Id on a
        # repeated POST with the same LeanRunId; we simulate that by returning
        # the same id from both responses.
        _write_workspace(tmp_path, "run_dup")

        async with respx.mock(base_url=_BACKEND_URL, assert_all_called=True) as mock:
            route = mock.post("/api/backtest-runs/persist-lean").mock(
                return_value=httpx.Response(200, json={"strategy_execution_id": 7})
            )

            first = await backfill_directory(tmp_path, _BACKEND_URL)
            assert first == [7]
            assert route.call_count == 1

            second = await backfill_directory(tmp_path, _BACKEND_URL)
            assert second == [7]
            assert route.call_count == 2  # called again, but backend dedupes

    @pytest.mark.asyncio
    async def test_skips_incomplete_workspaces_and_persists_the_rest(self, tmp_path: Path) -> None:
        _write_workspace(tmp_path, "run_ok")
        _write_workspace(tmp_path, "run_incomplete", include_normalized=False)
        # Loose file at the root — should be ignored (only dirs are workspaces)
        (tmp_path / "stray.txt").write_text("not a workspace")

        async with respx.mock(base_url=_BACKEND_URL, assert_all_called=True) as mock:
            mock.post("/api/backtest-runs/persist-lean").mock(
                return_value=httpx.Response(200, json={"strategy_execution_id": 42})
            )

            persisted_ids = await backfill_directory(tmp_path, _BACKEND_URL)

            assert persisted_ids == [42]

    @pytest.mark.asyncio
    async def test_continues_when_backend_returns_5xx(self, tmp_path: Path) -> None:
        _write_workspace(tmp_path, "run_500")
        _write_workspace(tmp_path, "run_ok")

        responses = iter(
            [
                httpx.Response(500, json={"error": "boom"}),
                httpx.Response(200, json={"strategy_execution_id": 8}),
            ]
        )

        async with respx.mock(base_url=_BACKEND_URL, assert_all_called=True) as mock:
            mock.post("/api/backtest-runs/persist-lean").mock(side_effect=lambda req: next(responses))

            persisted_ids = await backfill_directory(tmp_path, _BACKEND_URL)

            # Failed POST is logged + skipped; the OK one is persisted.
            assert persisted_ids == [8]

    @pytest.mark.asyncio
    async def test_missing_artifacts_root_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"

        with pytest.raises(FileNotFoundError, match="artifacts_root"):
            await backfill_directory(missing, _BACKEND_URL)

    @pytest.mark.asyncio
    async def test_empty_artifacts_root_returns_empty_list(self, tmp_path: Path) -> None:
        persisted_ids = await backfill_directory(tmp_path, _BACKEND_URL)
        assert persisted_ids == []
