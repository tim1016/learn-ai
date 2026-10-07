"""Generate the FEE-002 Alpaca option-fee golden fixture.

The oracle is hand-computed decimal arithmetic over the published rates
written out below. It never imports the canonical implementation. It writes
the three fixture files and prints their hashes; the manifest entry is edited
by hand. Run from ``PythonDataService/``:

    .venv/bin/python -m scripts.fixture_generators.alpaca_option_fees
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from app.lean_sidecar.trading_calendar import session_open_ms_utc

logger = logging.getLogger(__name__)

FIXTURE_ID = "FEE-002"
ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden" / "broker-fees" / FIXTURE_ID / "v1"

_TYPES = ("sec", "taf", "cat", "orf", "occ")

# Published rates in force on each case date. ``None`` = not pinned for that
# date; the oracle emits null and the model must too. ``cat`` is per
# executed-equivalent share; the others are per dollar of premium sold (sec)
# or per contract.
_RATES: dict[date, dict[str, Decimal | None]] = {
    # Before Alpaca's 2026-02-12 revision: only the regulators' own rates are pinned.
    date(2026, 1, 15): {"sec": Decimal("0"), "taf": Decimal("0.00329"), "cat": None, "orf": None, "occ": None},
    # Alpaca revision 2026-02-12 in force; SEC still $0.00 per million.
    date(2026, 4, 2): {
        "sec": Decimal("0"), "taf": Decimal("0.00329"), "cat": None,
        "orf": Decimal("0.02375"), "occ": Decimal("0.025"),
    },
    # SEC $20.60 per million from 2026-04-04; Alpaca's 2026-04-09 revision not yet out.
    date(2026, 4, 6): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0.00329"), "cat": None,
        "orf": Decimal("0.02375"), "occ": Decimal("0.025"),
    },
    # Alpaca revision 2026-04-09: ORF $0.02295.
    date(2026, 6, 30): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0.00329"), "cat": None,
        "orf": Decimal("0.02295"), "occ": Decimal("0.025"),
    },
    # Alpaca revision 2026-07-01: ORF $0.015, CAT listed under Options.
    date(2026, 7, 1): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0.00329"), "cat": Decimal("0.000003"),
        "orf": Decimal("0.015"), "occ": Decimal("0.025"),
    },
    date(2026, 9, 30): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0.00329"), "cat": Decimal("0.000003"),
        "orf": Decimal("0.015"), "occ": Decimal("0.025"),
    },
    # FINRA pauses the TAF from 2026-10-01 through 2026-12-31.
    date(2026, 10, 1): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0"), "cat": Decimal("0.000003"),
        "orf": Decimal("0.015"), "occ": Decimal("0.025"),
    },
    date(2026, 10, 5): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0"), "cat": Decimal("0.000003"),
        "orf": Decimal("0.015"), "occ": Decimal("0.025"),
    },
    # The TAF resumes on 2027-01-01.
    date(2027, 1, 4): {
        "sec": Decimal("0.0000206"), "taf": Decimal("0.00329"), "cat": Decimal("0.000003"),
        "orf": Decimal("0.015"), "occ": Decimal("0.025"),
    },
}

# (case, trade date, side, contracts, premium per share, multiplier)
_CASES: tuple[tuple[str, date, str, str, str, int], ...] = (
    ("buy_2_at_1_50", date(2026, 10, 5), "buy", "2", "1.50", 100),
    ("sell_2_at_2_10", date(2026, 10, 5), "sell", "2", "2.10", 100),
    ("sell_10_of_a_ten_share_contract", date(2026, 10, 5), "sell", "10", "0.55", 10),
    ("spread_bought_leg", date(2026, 10, 5), "buy", "1", "4.10", 100),
    ("spread_sold_leg", date(2026, 10, 5), "sell", "1", "2.20", 100),
    ("sell_2_at_2_10_last_day_before_taf_pause", date(2026, 9, 30), "sell", "2", "2.10", 100),
    ("sell_2_at_2_10_first_day_of_taf_pause", date(2026, 10, 1), "sell", "2", "2.10", 100),
    ("sell_2_at_2_10_taf_resumed", date(2027, 1, 4), "sell", "2", "2.10", 100),
    ("buy_5_last_day_of_old_orf", date(2026, 6, 30), "buy", "5", "3.00", 100),
    ("buy_5_first_day_of_new_orf", date(2026, 7, 1), "buy", "5", "3.00", 100),
    ("sell_1_before_sec_fee_resumed", date(2026, 4, 2), "sell", "1", "5.00", 100),
    ("sell_1_after_sec_fee_resumed", date(2026, 4, 6), "sell", "1", "5.00", 100),
    ("buy_1_before_alpaca_rows", date(2026, 1, 15), "buy", "1", "1.00", 100),
    ("sell_1_before_alpaca_rows", date(2026, 1, 15), "sell", "1", "1.00", 100),
)

# Days settled on their own: every fee type summed, then rounded up to the cent.
_SESSIONS: dict[str, tuple[str, ...]] = {
    "three_fills_on_2026_10_05": ("buy_2_at_1_50", "sell_2_at_2_10", "sell_10_of_a_ten_share_contract"),
    "one_sale_last_day_before_taf_pause": ("sell_2_at_2_10_last_day_before_taf_pause",),
    "one_sale_first_day_of_taf_pause": ("sell_2_at_2_10_first_day_of_taf_pause",),
}
# An entry's own fills, settled as a day of their own, in whole cents.
_PROVISIONS: dict[str, tuple[str, ...]] = {
    "one_bull_call_spread": ("spread_bought_leg", "spread_sold_leg"),
    "two_contracts_bought": ("buy_2_at_1_50",),
}


def _oracle(day: date, side: str, contracts: Decimal, premium: Decimal, multiplier: int) -> dict[str, Decimal | None]:
    rates = _RATES[day]
    fees: dict[str, Decimal | None] = {
        "cat": None if rates["cat"] is None else contracts * multiplier * rates["cat"],
        "orf": None if rates["orf"] is None else contracts * rates["orf"],
        "occ": None if rates["occ"] is None else contracts * rates["occ"],
    }
    if side == "buy":
        return {"sec": Decimal("0"), "taf": Decimal("0"), **fees}
    return {
        "sec": None if rates["sec"] is None else contracts * premium * multiplier * rates["sec"],
        "taf": None if rates["taf"] is None else contracts * rates["taf"],
        **fees,
    }


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ceil_cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _settle(fees: list[dict[str, Decimal | None]]) -> dict[str, Decimal]:
    settled: dict[str, Decimal] = {}
    for fee_type in _TYPES:
        accruals = [fill[fee_type] for fill in fees]
        assert all(accrual is not None for accrual in accruals), "a settled day holds only pinned rates"
        settled[fee_type] = _ceil_cents(sum(accruals, Decimal("0")))
    return settled


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hashes(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    normalized = json.dumps(
        json.loads(raw.decode("utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest(), hashlib.sha256(raw).hexdigest()


_ATTRIBUTION = """# FEE-002 — Alpaca option regulatory and clearing pass-through fees

## Source

Read on 2026-10-06. No account, order or broker API call was used: every rate is from a published document.

| Fee type | Unit, side | Rate, and the date it is in force from here | Where it is stated |
|---|---|---|---|
| SEC §31 | per dollar of premium sold; sells | `0` from 2025-05-14; `0.0000206` ($20.60 per $1M) from 2026-04-04 | SEC Fee Rate Advisories 2025-2 and 2026-2 (https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2). A sale of an OCC-issued put or call is a covered sale: 17 CFR 240.31. The model reads this rate from the stock fee model's table. |
| FINRA TAF | per contract sold; sells; no per-trade cap on options | `0.00329` for 2026; `0` from 2026-10-01 through 2026-12-31; `0.00329` again from 2027-01-01 through 2028; `0.00390` from 2029, `0.00404` from 2030, `0.00420` from 2031 | SR-FINRA-2026-020, 91 FR 60427 (https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-020-91-FR-60427.pdf) for the ladder; SR-FINRA-2026-021, SEC Release 34-106409, Exhibits 5A and 5B (https://www.sec.gov/files/rules/sro/finra/2026/34-106409-ex5.pdf) for the pause and the resumption. |
| ORF | per contract; buys and sells | `0.02375` (revision of 2026-02-12); `0.02295` (2026-04-09); `0.015` (2026-07-01, still printed 2026-10-01) | Alpaca Securities LLC, Brokerage Fee Schedule, "Pass-Through Regulatory and Exchange Fees", Options table (https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf, "Revised on October 1, 2026"). Earlier revisions: Internet Archive captures `20260219160225`, `20260410195706`, `20260720131641` and `20260919034252` of the same URL. |
| OCC clearing | per contract; buys and sells | `0.025`, printed "(no cap)" (revision of 2026-02-12, unchanged since) | same schedule and captures |
| FINRA CAT | per executed-equivalent share; buys and sells; one contract counts its multiplier (100, "or applicable multiplier") | `0.000003` (first listed under Options in the revision of 2026-07-01) | same schedule; the conversion is also in SEC Release 34-105449 (SR-FINRA-2026-010). |

Settlement is from the same schedule, "How fees are charged" (in the revisions of 2026-07-01 and later): fees use the exact executed quantity; each fee type is summed separately per account per day; each total is then rounded up to the next cent.

## What these dates are, and are not

- **SEC and TAF dates are the regulators' effective dates.**
- **ORF, OCC and CAT dates are Alpaca's revision dates.** Alpaca states no effective dates, so the day it began charging each figure is not known. A case dated near one of these steps shows what the model does, not what Alpaca charged that day.
- Alpaca calls its ORF figure an average of what it remits to the exchanges.
- Before 2026-02-12 the ORF and OCC rows are not pinned, and before 2026-07-01 the CAT row is not: the model answers `null` there, never zero. Alpaca's earlier schedules print other figures (ORF `0.02685`, OCC `0.02` with a $55 cap), with no date to hang them on.
- **This is documentation, not a recording.** No option fee activity from an Alpaca account has been seen. Whether Alpaca posts option fees as these five types, on which days, and how a day that also traded stock rounds SEC, TAF and CAT is unverified. Replace this fixture's expectations with a recorded day when one exists.

## Independent numerical oracle

`reference_kind=hand_computed`. Expected values are exact decimal arithmetic over the literal rates in the generator, computed without importing `app.broker.alpaca.option_fees`. Sells: `sec = contracts × premium × multiplier × r_sec`, `taf = contracts × r_taf`. Both sides: `cat = contracts × multiplier × r_cat`, `orf = contracts × r_orf`, `occ = contracts × r_occ`. A buy owes no SEC fee and no TAF. A fee type with no pinned rate on the case date is `null`.

## Cases

- `buy_2_at_1_50` (2026-10-05): cat `2 × 100 × 0.000003 = 0.000600`, orf `0.030`, occ `0.050`.
- `sell_2_at_2_10` (2026-10-05): sec `2 × 2.10 × 100 × 0.0000206 = 0.0086520`, taf `0` (paused), cat `0.000600`, orf `0.030`, occ `0.050`.
- `sell_10_of_a_ten_share_contract` (2026-10-05): a multiplier of 10. sec on `10 × 0.55 × 10 = $55.00` of premium, `0.00113300`; cat `10 × 10 × 0.000003 = 0.000300`; orf `0.150`; occ `0.250`.
- **A rate change straddled:** the same sale of 2 at $2.10 on 2026-09-30 (taf `2 × 0.00329 = 0.00658`), on 2026-10-01 (taf `0`), and on 2027-01-04 (taf `0.00658` again). Settled as days of their own, 2026-09-30 costs `0.01 + 0.01 + 0.01 + 0.03 + 0.05 = 0.11` and 2026-10-01 costs `0.01 + 0.00 + 0.01 + 0.03 + 0.05 = 0.10`.
- `buy_5_last_day_of_old_orf` (2026-06-30) and `buy_5_first_day_of_new_orf` (2026-07-01): orf `5 × 0.02295 = 0.11475` then `5 × 0.015 = 0.075`; cat `null` then `0.001500`.
- `sell_1_before_sec_fee_resumed` (2026-04-02; 2026-04-03 was Good Friday) and `sell_1_after_sec_fee_resumed` (2026-04-06): sec `0` then `500 × 0.0000206 = 0.0103000`.
- `buy_1_before_alpaca_rows` and `sell_1_before_alpaca_rows` (2026-01-15): orf, occ and cat `null`; the sale's sec is a pinned `0` and its taf `0.00329`.
- Day `three_fills_on_2026_10_05`: sec `0.0086520 + 0.00113300 = 0.00978500 → 0.01`; taf `0 → 0.00`; cat `0.000600 + 0.000600 + 0.000300 = 0.001500 → 0.01`; orf `0.210 → 0.21`; occ `0.350 → 0.35`; total `0.58`.
- Provision `one_bull_call_spread` (buy 1 at $4.10, sell 1 at $2.20, 2026-10-05): sec `220 × 0.0000206 = 0.0045320 → 0.01`; taf `0.00`; cat `0.000600 → 0.01`; orf `0.030 → 0.03`; occ `0.050 → 0.05`; `10` cents.
- Provision `two_contracts_bought`: cat `0.01`, orf `0.03`, occ `0.05`; `9` cents.

## Timestamps

`trade_date_ms` is the ET session-open anchor of the trade date (`session_open_ms_utc`), `int64 ms UTC`; the test derives the ET date with `et_date_at_ms`.

## Tolerance

`atol=0, rtol=0`: the model and the oracle are both exact `Decimal` arithmetic; the test compares `Decimal` values for equality, and provisions as whole cents.

## Regeneration

`cd PythonDataService && .venv/bin/python -m scripts.fixture_generators.alpaca_option_fees`, then put the printed hashes in `tests/fixtures/golden/manifest.json` by hand. Regenerate only when a cited source changes, and say which in the commit.
"""


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    input_path = FIXTURE_DIR / "input.json"
    output_path = FIXTURE_DIR / "output.json"
    attribution_path = FIXTURE_DIR / "attribution.md"

    fills_in: list[dict[str, Any]] = []
    fees_by_case: dict[str, dict[str, Decimal | None]] = {}
    for name, day, side, contracts_text, premium_text, multiplier in _CASES:
        fees_by_case[name] = _oracle(day, side, Decimal(contracts_text), Decimal(premium_text), multiplier)
        fills_in.append(
            {
                "case": name,
                "trade_date_ms": session_open_ms_utc(day),
                "side": side,
                "contracts": contracts_text,
                "premium": premium_text,
                "multiplier": multiplier,
            }
        )
    sessions_out: dict[str, dict[str, str]] = {}
    for name, cases in _SESSIONS.items():
        settled = _settle([fees_by_case[case] for case in cases])
        sessions_out[name] = {
            **{fee_type: str(amount) for fee_type, amount in settled.items()},
            "total": str(sum(settled.values(), Decimal("0"))),
        }
    provisions_out = {
        name: int(sum(_settle([fees_by_case[case] for case in cases]).values(), Decimal("0")) * 100)
        for name, cases in _PROVISIONS.items()
    }

    _write_json(
        input_path,
        {
            "fills": fills_in,
            "sessions": {name: list(cases) for name, cases in _SESSIONS.items()},
            "provisions": {name: list(cases) for name, cases in _PROVISIONS.items()},
        },
    )
    _write_json(
        output_path,
        {
            "fills": {
                name: {fee_type: _text(fees[fee_type]) for fee_type in _TYPES} for name, fees in fees_by_case.items()
            },
            "sessions": sessions_out,
            "provision_cents": provisions_out,
        },
    )
    attribution_path.write_text(_ATTRIBUTION, encoding="utf-8")

    for path in (input_path, output_path):
        content_hash, file_hash = _hashes(path)
        logger.info("%s content_sha256=%s file_sha256=%s", path.name, content_hash, file_hash)
    logger.info(
        "%s file_sha256=%s", attribution_path.name, hashlib.sha256(attribution_path.read_bytes()).hexdigest()
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
