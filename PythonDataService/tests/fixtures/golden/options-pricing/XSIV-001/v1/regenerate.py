"""Record, and freeze the provider path's output for, ``XSIV-001`` (#3540, Part B of the Cross-sectional study's move to the lake).

Fixture paperwork: run by hand, never by a test or by CI. Two steps, both from ``PythonDataService/`` with
``PYTHONPATH=.`` and the repo's venv:

    python tests/fixtures/golden/options-pricing/XSIV-001/v1/regenerate.py payload <input.json>
    python tests/fixtures/golden/options-pricing/XSIV-001/v1/regenerate.py freeze <input.json> <output.json>

``payload`` writes the chain payload: a pinned listed chain (``tests/_helpers/option_chain.OptionChain``: weekly Friday
expiries, three of them listed late, a one-dollar strike grid), the stock rows the study hands the IV build, and the
daily option price of every contract the provider path asked for on every day it priced. It runs on any commit that
holds the generator.

``freeze`` runs the **provider path** over that payload and writes what it returns. The provider path is the code the
study used before the option inputs moved to the lake: ``PolygonClientService`` over the Polygon SDK client's
``list_options_contracts`` and ``list_aggs``, the contract finder that asked it for expiries and contracts, and the IV
builder that fetched daily option bars. That code, and the generator's SDK surface, were deleted in the commit that
moved the study's option inputs to the lake, so ``freeze`` only runs on commit 8c9bba4a (the tip of #3540 Part A,
before Part B) or the commit that adds this fixture: ``git worktree add <dir> 8c9bba4a``, copy this folder in, run it
there. The output on file was written on the fixture's own commit, with no other change to the provider path.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

FIXTURE = "XSIV-001"
UNDERLYING = "SPY"
#: October and November 2024 up to Thanksgiving: every weekday is a session. One expiry lookup per month serves it, and
#: the late-listed weeklies below force the finder to list again inside October.
WINDOW_START, WINDOW_DAYS = date(2024, 10, 1), 58
STEP = 0.1  # a slow walk, so a strike chosen at an expiry's first use is still near the money when it leaves the pair
LISTED_WEEKS_AHEAD = 6  # an expiry from this Friday on is listed this many weeks before it expires


def window() -> list[date]:
    return [d for d in (WINDOW_START + timedelta(days=n) for n in range(WINDOW_DAYS)) if d.weekday() < 5]


def stock_rows(days: list[date]) -> list[dict]:
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    return [
        {
            "timestamp": session_open_ms_utc(day),
            "open": 150.0 + STEP * i,
            "high": 150.0 + STEP * i + 1.0,
            "low": 150.0 + STEP * i - 1.0,
            "close": round(150.0 + STEP * i, 4),
            "volume": 1_000_000.0,
        }
        for i, day in enumerate(days)
    ]


def sigma_table(days: list[date]) -> dict[str, float]:
    from tests._helpers.option_chain import default_sigma

    return {day.isoformat(): default_sigma(day) for day in days}


def chain_from(payload: dict):
    from tests._helpers.option_chain import OptionChain

    table = payload["sigma"]
    return OptionChain(
        payload["underlying"],
        {date.fromisoformat(day): close for day, close in payload["closes"].items()},
        rate=payload["rate"],
        sigma=lambda day: table[day.isoformat()],
        strike_step=payload["strike_step"],
        expiries=tuple(date.fromisoformat(e) for e in payload["expiries"]),
        listed_on={date.fromisoformat(e): date.fromisoformat(d) for e, d in payload["listed_on"].items()},
    )


def provider_path(payload: dict):
    """The study's provider path over the payload's chain: the contract finder, then the IV builder, as the study called them.

    Each is given a chain of its own, so each chain's request log is that one call's. Returns the two frames and the two chains.
    """
    from app.research.options.contract_finder import find_bracket_contracts
    from app.research.options.iv_builder import build_iv_history
    from app.research.options.option_input_ledger import OptionInputLedger
    from app.services.polygon_client import PolygonClientService

    def service(chain):
        wrapper = PolygonClientService()
        wrapper.client = chain  # type: ignore[assignment]
        return wrapper

    found_chain, built_chain = chain_from(payload), chain_from(payload)
    window = payload["window"]
    bracket = find_bracket_contracts(
        payload["underlying"], window[0], window[-1], service(found_chain), payload["stock_rows"], OptionInputLedger()
    )
    iv = build_iv_history(
        payload["underlying"], window[0], window[-1], service(built_chain), payload["stock_rows"], OptionInputLedger()
    )
    return bracket, iv, found_chain, built_chain


def records(frame) -> list[dict]:
    """A frame as JSON records: a missing value is ``null``; every float keeps the digits Python prints for it."""
    out = []
    for row in frame.to_dict("records"):
        out.append({key: (None if value is None or value != value else value) for key, value in row.items()})
    return out


def make_payload(path: Path) -> None:
    from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
    from app.utils.session_anchors import et_midnight_ms
    from tests._helpers.option_chain import OptionChain

    days = window()
    rows = stock_rows(days)
    closes = {day: row["close"] for day, row in zip(days, rows, strict=True)}
    probe = OptionChain(UNDERLYING, closes, rate=DEFAULT_RISK_FREE_RATE)
    expiries = probe._expiries()
    listed_on = {e: e - timedelta(weeks=LISTED_WEEKS_AHEAD) for e in expiries if e - timedelta(weeks=LISTED_WEEKS_AHEAD) > days[0]}
    payload = {
        "fixture": FIXTURE,
        "underlying": UNDERLYING,
        "rate": DEFAULT_RISK_FREE_RATE,
        "strike_step": 1.0,
        "window": [d.isoformat() for d in days],
        "closes": {d.isoformat(): c for d, c in closes.items()},
        "stock_rows": rows,
        "sigma": sigma_table(days),
        "expiries": [e.isoformat() for e in expiries],
        "listed_on": {e.isoformat(): d.isoformat() for e, d in listed_on.items()},
        "strikes": probe._strikes(),
    }
    chain = provider_path(payload)[3]
    priced: dict[str, dict[str, float]] = {}
    by_ms = {et_midnight_ms(d): d.isoformat() for d in days}
    for ticker in sorted(set(chain.bar_requests)):
        bars = chain.list_aggs(ticker=ticker, multiplier=1, timespan="day", from_=days[0].isoformat(), to=days[-1].isoformat())
        priced[ticker] = {by_ms[bar.timestamp]: bar.close for bar in bars}
    payload["priced_bars"] = priced
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def freeze(payload_path: Path, output_path: Path) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    bracket, iv, found_chain, built_chain = provider_path(payload)
    asked = sorted(set(built_chain.bar_requests))
    assert asked == sorted(payload["priced_bars"]), "the payload's priced contracts are the ones the provider path asked for"
    output = {
        "fixture": FIXTURE,
        "bracket": records(bracket),
        "iv": records(iv),
        "expiry_lookups_as_of": [r["as_of"] for r in found_chain.contract_requests if r.get("expiration_date") is None],
        "contract_lookups_expiry": [r["expiration_date"] for r in found_chain.contract_requests if r.get("expiration_date") is not None],
        "contracts_priced": asked,
    }
    output_path.write_text(json.dumps(output, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    step, *paths = sys.argv[1:]
    if step == "payload" and len(paths) == 1:
        make_payload(Path(paths[0]))
    elif step == "freeze" and len(paths) == 2:
        freeze(Path(paths[0]), Path(paths[1]))
    else:
        raise SystemExit(__doc__)
