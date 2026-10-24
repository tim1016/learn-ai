"""The Cross-sectional study's option inputs: the provider path against the lake path, over one pinned payload (#3540 Part B; R14).

Golden fixture ``XSIV-001`` holds the provider path's output, frozen before the option inputs moved to the lake. This commit
only proves the freeze: the provider path, still in the tree, returns the frozen frames to the bit. The lake path is held
to the same frames by the commit that replaces the provider path.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.research.options.contract_finder import find_bracket_contracts
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_input_ledger import OptionInputLedger
from app.services.polygon_client import PolygonClientService
from tests._helpers.option_chain import OptionChain
from tests.fixtures.golden_support.registry import default as registry

FIXTURE = "XSIV-001"


def payload() -> dict:
    return json.loads(registry.resolve(FIXTURE, "input").read_text(encoding="utf-8"))


def frozen() -> dict:
    return json.loads(registry.resolve(FIXTURE, "output").read_text(encoding="utf-8"))


def chain_of(data: dict) -> OptionChain:
    table = data["sigma"]
    return OptionChain(
        data["underlying"],
        {date.fromisoformat(day): close for day, close in data["closes"].items()},
        rate=data["rate"],
        sigma=lambda day: table[day.isoformat()],
        strike_step=data["strike_step"],
        expiries=tuple(date.fromisoformat(e) for e in data["expiries"]),
        listed_on={date.fromisoformat(e): date.fromisoformat(d) for e, d in data["listed_on"].items()},
    )


def records(frame: pd.DataFrame) -> list[dict]:
    return [{k: (None if v is None or v != v else v) for k, v in row.items()} for row in frame.to_dict("records")]


@pytest.fixture(autouse=True)
def no_fred(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)


def test_the_frozen_frames_are_what_the_provider_path_returns():
    data, expected = payload(), frozen()
    service = PolygonClientService()
    service.client = chain_of(data)  # type: ignore[assignment]
    first, last = data["window"][0], data["window"][-1]

    bracket = find_bracket_contracts(data["underlying"], first, last, service, data["stock_rows"], OptionInputLedger())
    iv = build_iv_history(data["underlying"], first, last, service, data["stock_rows"], OptionInputLedger())

    assert records(bracket) == expected["bracket"], "the contract finder's frame, every column, to the bit"
    assert records(iv) == expected["iv"], "the IV builder's frame, every column, to the bit"
    assert len(expected["bracket"]) == len(expected["iv"]) == len(data["window"])
    assert expected["expiry_lookups_as_of"][1:5] == ["2024-10-10", "2024-10-17", "2024-10-24", "2024-10-31"], "the late listings forced re-lists"
