"""Read-only OpenAPI anchor for the committed Alpaca diagnostic fixtures.

The Angular gallery imports the documents from ``contracts/`` and never calls
this route.  Referencing the envelope here keeps Python the contract authority
and lets the existing OpenAPI generator derive the client type mechanically.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from app.schemas.alpaca_bot_control_example import AlpacaBotControlFixtureEnvelope

router = APIRouter(prefix="/api/examples/alpaca-bot-control", tags=["examples"])

_FIXTURES_ROOT = (
    Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "alpaca-bot-control" / "v1"
)


def _load_fixtures() -> list[AlpacaBotControlFixtureEnvelope]:
    """Validate every committed document before exposing it as a contract sample."""
    return [
        AlpacaBotControlFixtureEnvelope.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        for path in sorted(_FIXTURES_ROOT.glob("*.json"))
    ]


@router.get("/fixtures", response_model=list[AlpacaBotControlFixtureEnvelope])
async def list_static_fixtures() -> list[AlpacaBotControlFixtureEnvelope]:
    """Return the committed documents for contract inspection only."""
    return _load_fixtures()
