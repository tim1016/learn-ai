"""Read one experiment lane through the verified fleet routing boundary.

Every pass starts at sequence zero to observe changes to older receipts. Pages
are bounded and use one fixed upper sequence so a running bot cannot extend the
walk indefinitely. Each page is a committed source observation; this does not
claim a cross-request database snapshot or market-data continuity.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id
from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.broker.fleet.routing import LaneRouter
from app.schemas.paper_live_experiments import (
    ClerkDecisionEvidencePage,
    ExperimentDecision,
    ExperimentEvidenceCapture,
    ExperimentLane,
    ExperimentSource,
)
from app.utils.timestamps import Clock, now_ms_utc

_OPERATION = next(operation for operation in ALPACA_OPERATIONS if operation.operation_id == "bot_decision_evidence")
_MAX_PAGES = 100


class ExperimentEvidenceUnavailable(ValueError):
    """A read refused or could not prove the frozen experiment provenance."""


class PaperLiveEvidenceReader:
    """Coordinator-side read client; never dispatches a broker command."""

    def __init__(self, router: LaneRouter, *, clock: Clock = now_ms_utc) -> None:
        self._router = router
        self._clock = clock

    async def read(self, source: ExperimentSource, *, lane: ExperimentLane) -> ExperimentEvidenceCapture:
        captured_at_ms = self._clock()
        after_seq = 0
        highest_seq: int | None = None
        first_page: ClerkDecisionEvidencePage | None = None
        decisions: list[ExperimentDecision] = []
        for _ in range(_MAX_PAGES):
            query = {"after_seq": str(after_seq), "limit": "500"}
            if highest_seq is not None:
                query["through_seq"] = str(highest_seq)
            result = await self._router.deliver_read(
                broker="alpaca",
                clerk_id=source.clerk_id,
                operation=_OPERATION,
                path_params={"account_id": source.account_id, "sid": source.strategy_instance_id},
                query=query,
            )
            if result.status_code != 200:
                raise ExperimentEvidenceUnavailable(f"The lane refused decision evidence (HTTP {result.status_code})")
            headers = {key.lower(): value for key, value in result.headers.items()}
            if (
                headers.get("x-fleet-broker") != "alpaca"
                or headers.get("x-fleet-clerk-id") != source.clerk_id
                or headers.get("x-fleet-binding-generation") != str(source.binding_generation)
            ):
                raise ExperimentEvidenceUnavailable("The lane's routing identity differs from the frozen experiment")
            try:
                page = ClerkDecisionEvidencePage.model_validate_json(result.body)
            except ValueError as exc:
                raise ExperimentEvidenceUnavailable("The lane returned invalid decision evidence") from exc
            if (
                canonical_alpaca_account_id(page.account_id) != canonical_alpaca_account_id(source.account_id)
                or page.strategy_instance_id != source.strategy_instance_id
                or page.db_identity_token != source.db_identity_token
                or page.account_mode != lane
                or page.authority_kind != "sqlite"
            ):
                raise ExperimentEvidenceUnavailable("The decision evidence source differs from the frozen experiment")
            if page.after_seq != after_seq or (highest_seq is not None and page.highest_seq != highest_seq):
                raise ExperimentEvidenceUnavailable("The lane changed the requested evidence sequence bounds")
            if first_page is not None and (
                page.config_hash != first_page.config_hash
                or page.authority_generation != first_page.authority_generation
            ):
                raise ExperimentEvidenceUnavailable("The lane's configuration or authority changed during collection")
            first_page = first_page or page
            highest_seq = page.highest_seq
            decisions.extend(page.decisions)
            if page.next_after_seq is None:
                return ExperimentEvidenceCapture(
                    source=source,
                    captured_at_ms=captured_at_ms,
                    highest_seq=highest_seq,
                    decisions=tuple(decisions),
                )
            after_seq = page.next_after_seq
        raise ExperimentEvidenceUnavailable("The decision evidence exceeds this collection pass's page budget")
