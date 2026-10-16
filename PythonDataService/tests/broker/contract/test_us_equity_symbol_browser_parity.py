"""Keep browser symbol checks pinned to the broker contract model."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.broker.contract.models import BrokerOrderLeg

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "Frontend/src/app/shared/symbol-catalog/us-equity-symbol.parity.json"
)


def test_browser_symbol_fixture_matches_broker_order_leg() -> None:
    fixture = json.loads(_FIXTURE.read_text())
    assert fixture["schema_version"] == 1
    assert fixture["canonical"] == "PythonDataService/app/broker/contract/models.py::BrokerOrderLeg.symbol"
    for case in fixture["cases"]:
        try:
            BrokerOrderLeg(symbol=case["symbol"], side="buy", quantity=1)
        except ValidationError:
            accepted = False
        else:
            accepted = True
        assert accepted is case["accepted"], case["symbol"]
