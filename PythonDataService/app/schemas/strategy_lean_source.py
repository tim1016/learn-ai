"""HTTP contract for a registered strategy's LEAN validation source."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.services.strategy_lean_source_service import StrategyLeanSource


class StrategyLeanSourceResponse(BaseModel):
    """Launcher-independent QCAlgorithm source exposed to Strategy Lab."""

    strategy_name: str
    template: str
    language: Literal["python"] = "python"
    source: str
    source_sha256: str

    @classmethod
    def from_strategy_source(cls, value: StrategyLeanSource) -> StrategyLeanSourceResponse:
        return cls(
            strategy_name=value.strategy_name,
            template=value.template,
            source=value.source,
            source_sha256=value.source_sha256,
        )
