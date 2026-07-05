from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ValidationState = Literal["validated", "needs_validation"]


class StrategyValidationDiagnostics(BaseModel):
    verdict: str
    trades_matched: int = Field(ge=0)
    trades_validated: int = Field(ge=0)
    pnl_max_abs_diff: str
    divergence_counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StrategyValidationEntry(BaseModel):
    strategy_key: str
    display_name: str
    description: str
    validation_state: ValidationState
    deployable: bool
    settings_file_ref: str | None = None
    settings_file_sha256: str | None = None
    qc_cloud_backtest_id: str | None = None
    audit_copy_ref: str | None = None
    audit_copy_sha256: str | None = None
    reconciliation_ref: str | None = None
    validation_case_symbol: str | None = None
    reconciliation_status: str | None = None
    diagnostics: StrategyValidationDiagnostics | None = None


class StrategyReferenceCode(BaseModel):
    path: str
    sha256: str
    language: str = "python"
    source: str


class StrategyValidationDetail(StrategyValidationEntry):
    reference_code: StrategyReferenceCode | None = None


class StrategyValidationCatalog(BaseModel):
    strategies: list[StrategyValidationEntry]
