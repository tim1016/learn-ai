"""The authenticated Clerk-to-coordinator Options replay request (#3368)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.broker_v2_panel import ChartHistoryResponse, ChartLiveResponse
from app.schemas.option_chart import ChartPremiumPane
from app.schemas.options_bot import OptionsBotConfiguration
from app.utils.session_anchors import MAX_TIMESTAMP_MS

Moment = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class OptionsOverlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    clerk_id: str = Field(min_length=1, max_length=128)
    configuration: OptionsBotConfiguration
    chart: ChartLiveResponse | ChartHistoryResponse
    session_opens: tuple[Moment, ...]
    now_ms: Moment


class OptionsOverlayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_instance_id: str
    golden_setup_id: str
    setup_sha256: str
    rules_digest: str
    session_opens: tuple[Moment, ...]
    premium_panes: list[ChartPremiumPane]
    backtest_notice: str | None

    @classmethod
    def from_engine_result(
        cls, request: OptionsOverlayRequest, chart: ChartLiveResponse | ChartHistoryResponse,
    ) -> OptionsOverlayResponse:
        return cls(strategy_instance_id=chart.strategy_instance_id,
                   golden_setup_id=request.configuration.golden_setup_id,
                   setup_sha256=request.configuration.setup_sha256, rules_digest=request.configuration.rules_digest,
                   session_opens=request.session_opens, premium_panes=chart.premium_panes, backtest_notice=chart.backtest_notice)
