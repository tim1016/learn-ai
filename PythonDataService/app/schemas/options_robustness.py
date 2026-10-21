"""Descriptive retained Options metrics and declared one-dimension neighbors (#3401)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.options_backtest_runs import OptionsBacktestCapitalEvidenceResponse
from app.schemas.options_research_lineage import OptionsLineageFinalist, OptionsLineageStress, OptionsLineageWindow
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionsRobustnessMetrics(BaseModel):
    net_profit_usd: str
    winning_sessions: int
    losing_sessions: int
    breakeven_sessions: int
    no_trade_sessions: int
    skipped_sessions: int
    refused_entries: int
    average_winning_session_usd: str | None
    average_losing_session_usd: str | None
    expectancy_usd: str | None
    largest_winning_session_usd: str | None
    largest_losing_session_usd: str | None
    profit_factor: str | None
    average_win_loss_ratio: str | None
    longest_winning_streak: int
    longest_losing_streak: int
    top_five_profit_contribution: str | None
    profit_without_best_session_usd: str | None
    peak_relative_drawdown_fraction: str
    session_end_drawdown_usd: str


class OptionsCandidateRobustness(BaseModel):
    study_id: str
    candidate_hash: str
    evidence_identity: str
    evidence_blob_sha256: str
    scope: Literal["development", "final"]
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    observed_sessions: int
    scheduled_sessions: int
    complete: bool
    metrics: OptionsRobustnessMetrics
    capital: OptionsBacktestCapitalEvidenceResponse | None
    cost_scenarios: list[OptionsLineageStress]
    final_window: OptionsLineageWindow
    exposed_final_outcome: OptionsLineageFinalist | None


class OptionsNeighbor(BaseModel):
    ordinal: int
    dimension: str
    label: str
    candidate_hash: str | None
    is_selected: bool
    state: Literal["retained", "untested", "invalid"]
    verdict: str | None
    net_profit_usd: str | None


class OptionsNeighborPage(BaseModel):
    study_id: str
    protocol_hash: str
    selected_candidate_hash: str
    scope: Literal["development"] = "development"
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    objective: str
    items: list[OptionsNeighbor]
    total: int
    offset: int
    limit: int
