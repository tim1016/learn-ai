"""Generate the FEE-001 Alpaca regulatory-fee golden fixture.

The oracle is hand-computed decimal arithmetic over the published rates. It
never imports the canonical implementation. Run from ``PythonDataService/``:

    .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from app.lean_sidecar.trading_calendar import session_open_ms_utc

FIXTURE_ID = "FEE-001"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "broker-fees" / FIXTURE_ID / "v1"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "golden" / "manifest.json"

# Published rates in force on each case date (docs/references/alpaca-regulatory-fees.md).
# ``None`` = not pinned for that date; the oracle emits null and the model must too.
_RATES: dict[date, dict[str, Decimal | None]] = {
    date(2026, 9, 8): {
        "sec": Decimal("0.0000206"),
        "taf": Decimal("0.000195"),
        "taf_cap": Decimal("9.79"),
        "cat": Decimal("0.000003"),
    },
    date(2025, 6, 2): {"sec": Decimal("0"), "taf": Decimal("0.000166"), "taf_cap": Decimal("8.30"), "cat": None},
    date(2024, 6, 3): {
        "sec": Decimal("0.0000278"),
        "taf": Decimal("0.000166"),
        "taf_cap": Decimal("8.30"),
        "cat": None,
    },
    date(2024, 3, 1): {"sec": None, "taf": Decimal("0.000166"), "taf_cap": Decimal("8.30"), "cat": None},
}

# (case, trade date, side, quantity, fill price)
_CASES: tuple[tuple[str, date, str, str, str], ...] = (
    ("sell_2026", date(2026, 9, 8), "sell", "100", "250.00"),
    ("buy_2026", date(2026, 9, 8), "buy", "100", "250.00"),
    ("sell_above_taf_cap", date(2026, 9, 8), "sell", "60000", "10.00"),
    ("sell_at_taf_cap_boundary", date(2026, 9, 8), "sell", "50205", "1.00"),
    ("sell_just_over_taf_cap", date(2026, 9, 8), "sell", "50206", "1.00"),
    ("fractional_buy", date(2026, 9, 8), "buy", "0.5", "400.00"),
    ("sell_sec_zero_regime_2025", date(2025, 6, 2), "sell", "100", "250.00"),
    ("sell_sec_2780_regime_2024", date(2024, 6, 3), "sell", "1000", "50.00"),
    ("sell_before_sec_pin_2024", date(2024, 3, 1), "sell", "10", "100.00"),
    ("buy_before_cat_pin_2025", date(2025, 6, 2), "buy", "10", "100.00"),
)
_SESSION_CASES = tuple(name for name, day, *_ in _CASES if day == date(2026, 9, 8))


def _oracle(day: date, side: str, quantity: Decimal, price: Decimal) -> dict[str, Decimal | None]:
    rates = _RATES[day]
    cat = None if rates["cat"] is None else quantity * rates["cat"]
    if side == "buy":
        return {"sec": Decimal("0"), "taf": Decimal("0"), "cat": cat}
    sec = None if rates["sec"] is None else quantity * price * rates["sec"]
    taf = None if rates["taf"] is None else min(quantity * rates["taf"], rates["taf_cap"])
    return {"sec": sec, "taf": taf, "cat": cat}


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ceil_cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hashes(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    normalized = json.dumps(
        json.loads(raw.decode("utf-8")),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest(), file_hash


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    input_path = FIXTURE_DIR / "input.json"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    fills_in: list[dict[str, Any]] = []
    fills_out: dict[str, dict[str, str | None]] = {}
    session_sums = {"sec": Decimal("0"), "taf": Decimal("0"), "cat": Decimal("0")}
    for name, day, side, quantity_text, price_text in _CASES:
        quantity, price = Decimal(quantity_text), Decimal(price_text)
        fees = _oracle(day, side, quantity, price)
        fills_in.append(
            {
                "case": name,
                "trade_date_ms": session_open_ms_utc(day),
                "side": side,
                "quantity": quantity_text,
                "fill_price": price_text,
            }
        )
        fills_out[name] = {key: _text(value) for key, value in fees.items()}
        if name in _SESSION_CASES:
            for key in session_sums:
                session_sums[key] += fees[key]  # every 2026-09-08 component is pinned
    settled = {key: _ceil_cents(value) for key, value in session_sums.items()}

    _write_json(
        input_path,
        {
            "fills": fills_in,
            "session": {"trade_date_ms": session_open_ms_utc(date(2026, 9, 8)), "cases": list(_SESSION_CASES)},
        },
    )
    _write_json(
        output_path,
        {
            "fills": fills_out,
            "session": {
                "sec": str(settled["sec"]),
                "taf": str(settled["taf"]),
                "cat": str(settled["cat"]),
                "total": str(sum(settled.values(), Decimal("0"))),
            },
        },
    )
    attribution_path.write_text(
        "# FEE-001 — Alpaca equity regulatory pass-through fees\n\n"
        "## Source\n\n"
        "Rates are the published pass-through schedule pinned on 2026-09-07: Alpaca "
        "Securities \"Broker Fee Schedule\" §\"Pass-Through Regulatory and Exchange Fees — "
        "Equities\" (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf), SEC "
        "Fee Rate Advisories 2024-2 / 2025-2 / 2026-2, and the FINRA SR-FINRA-2024-019 TAF "
        "fee-adjustment schedule. Row-by-row citations: docs/references/alpaca-regulatory-fees.md.\n\n"
        "## Independent numerical oracle\n\n"
        "`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the "
        "literal rates in this generator, computed without importing "
        "`app.broker.alpaca.regulatory_fees`. Sells: `sec = qty × price × r_sec`, "
        "`taf = min(qty × r_taf, cap)`, `cat = qty × r_cat`; buys owe only CAT. A component "
        "with no pinned rate on the case date is `null`, never zero.\n\n"
        "## Cases\n\n"
        "- Six 2026-09-08 fills (SEC $20.60/M, TAF $0.000195 cap $9.79, CAT $0.000003) including "
        "the TAF cap boundary: 50,205 shares → `9.789975` (under the cap), 50,206 → `9.79` (capped).\n"
        "- `sell_sec_zero_regime_2025` (2025-06-02): SEC `0` is a pinned zero; CAT unpinned → null.\n"
        "- `sell_sec_2780_regime_2024` (2024-06-03): SEC $27.80/M → `1.39` on $50,000.\n"
        "- `sell_before_sec_pin_2024` (2024-03-01): SEC unpinned → null; TAF pinned at 2024 rates.\n"
        "- Session settlement over the six 2026-09-08 fills: each component summed then rounded "
        "UP to the cent — sec `14.9434666 → 14.95`, taf `29.389475 → 29.39`, cat "
        "`0.4818345 → 0.49`, total `44.83`.\n\n"
        "## Timestamps\n\n"
        "`trade_date_ms` is the ET session-open anchor of the trade date "
        "(`session_open_ms_utc`), `int64 ms UTC`; the model derives the ET date with "
        "`et_date_at_ms`.\n\n"
        "## Tolerance\n\n"
        "`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the "
        "test compares `Decimal` values for equality.\n\n"
        "## Regeneration\n\n"
        "`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_regulatory_fees`\n",
        encoding="utf-8",
    )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["fixtures"] = [item for item in manifest["fixtures"] if item["id"] != FIXTURE_ID]
    content_hashes: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    for path in (input_path, output_path):
        content_hash, file_hash = _hashes(path)
        content_hashes[path.name] = content_hash
        file_hashes[path.name] = file_hash
    file_hashes[attribution_path.name] = hashlib.sha256(attribution_path.read_bytes()).hexdigest()
    manifest["fixtures"].append(
        {
            "id": FIXTURE_ID,
            "name": "Alpaca equity regulatory pass-through fees",
            "category": "broker-fees",
            "canonical_module": "PythonDataService/app/broker/alpaca/regulatory_fees.py",
            "canonical_callable": "fees_for_fill",
            "reference": {
                "kind": "hand_computed",
                "oracle": "exact decimal arithmetic over the published SEC/TAF/CAT rates without importing canonical code",
                "citation": "Alpaca Broker Fee Schedule (retrieved 2026-09-07), SEC fee-rate advisories 2024-2/2025-2/2026-2, FINRA SR-FINRA-2024-019; see docs/references/alpaca-regulatory-fees.md.",
            },
            "market_input": {
                "source": "synthetic",
                "vendor": None,
                "generated_by": "PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py",
            },
            "units": None,
            "tolerance": {
                "atol": 0.0,
                "rtol": 0.0,
                "note": "Exact Decimal oracle and exact Decimal model; compared for Decimal equality.",
            },
            "active_version": 1,
            "versions": {
                "1": {
                    "input": "input.json",
                    "output": "output.json",
                    "attribution": "attribution.md",
                    "content_sha256": content_hashes,
                    "file_sha256": file_hashes,
                }
            },
            "status": "active",
        }
    )
    _write_json(MANIFEST_PATH, manifest)


if __name__ == "__main__":
    main()
