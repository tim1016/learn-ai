"""Ask the lake-owning coordinator for an Options comparison; never open a Clerk-local lake (#3368)."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.broker.fleet.internal_http import FleetTransportRefused, build_internal_client, enforce_private_http_target
from app.broker.fleet.options_overlay import INTERNAL_OPTIONS_OVERLAY_PATH, OPTIONS_OVERLAY_READ_TIMEOUT_S
from app.config import fleet_settings
from app.schemas.broker_v2_panel import ChartHistoryResponse, ChartLiveResponse
from app.schemas.fleet_options_overlay import OptionsOverlayRequest, OptionsOverlayResponse
from app.schemas.options_bot import OptionsBotConfiguration

logger = logging.getLogger(__name__)


async def fetch_options_overlay(
    chart: ChartLiveResponse | ChartHistoryResponse, configuration: OptionsBotConfiguration,
    session_opens: tuple[int, ...], *, now_ms: int,
) -> ChartLiveResponse | ChartHistoryResponse:
    base, clerk, token = fleet_settings.COORDINATOR_URL, fleet_settings.CLERK_ID, fleet_settings.AGENT_SERVICE_TOKEN
    if not (base and clerk and token):
        return chart.model_copy(update={"backtest_notice": "Comparison not ready: the coordinator connection is not configured."})
    request = OptionsOverlayRequest(clerk_id=clerk, chart=chart, configuration=configuration,
                                   session_opens=session_opens, now_ms=now_ms)
    try:
        await asyncio.to_thread(enforce_private_http_target, base)
        async with build_internal_client(read_timeout_s=OPTIONS_OVERLAY_READ_TIMEOUT_S) as client:
            response = await client.post(f"{base.rstrip('/')}{INTERNAL_OPTIONS_OVERLAY_PATH}",
                json=request.model_dump(mode="json"), headers={"X-Fleet-Clerk-Id": clerk, "X-Fleet-Agent-Token": token})
        response.raise_for_status()
        answer = OptionsOverlayResponse.model_validate_json(response.content)
        if (answer.strategy_instance_id, answer.golden_setup_id, answer.setup_sha256, answer.rules_digest, answer.session_opens) != (
            chart.strategy_instance_id, configuration.golden_setup_id, configuration.setup_sha256, configuration.rules_digest, session_opens,
        ):
            raise ValueError("The coordinator answered a different comparison.")
    except (httpx.HTTPError, FleetTransportRefused, ValueError) as exc:
        logger.warning("Options comparison coordinator did not answer", extra={"action": "options_overlay_coordinator_unavailable", "error_type": type(exc).__name__})
        return chart.model_copy(update={"backtest_notice": "Comparison not ready: the coordinator could not provide this comparison."})
    return chart.model_copy(update={"premium_panes": answer.premium_panes, "backtest_notice": answer.backtest_notice})
