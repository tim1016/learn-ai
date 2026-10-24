# SPY option price rule: regular-session close versus vendor daily VWAP

## Market input provenance

The two unadjusted Polygon (Massive) daily aggregates were fetched with the owner's approval on 2026-10-24 and posted in
[#3524, Part B design](https://github.com/tim1016/learn-ai/issues/3524#issuecomment-6387331949). This fixture recovers the
JSON bodies from that comment, preserving their text with one trailing newline. It does not claim to reproduce the
original HTTP response bytes or headers. No vendor call was made to create this fixture. `recording.json` identifies
the source and files; the golden registry pins the exact files and their normalized JSON content.

The minute bars are the existing, separately recorded fixture `options-backtest-spy-2025-06-10/v1`, recorded on
2026-10-12. Those bars and this daily response concern the same contracts and session but were fetched on different
dates. A vendor revision could make a new recording differ. Both daily answers explicitly say `adjusted: false`.

## Methodology provenance

Owner decision D-A in #3524 prices an option at the close of its latest minute wholly within the regular session:
start >= open and start + 60,000 ms <= close. Volume is the sum of those minutes. On 2025-06-10 the session spans
13:30–20:00 UTC, so the last included minute starts at 19:59 UTC (15:59 New York). The minute stamped 16:00 and the
ETF-option tail are excluded. `app/research/options/option_inputs.py::regular_session_close` is the canonical fold.

The daily VWAP is observed vendor output, with an opaque trade aggregation methodology. This test proves the selected
price and its difference from that observation; it does not independently certify the vendor's VWAP calculation.

## Independent numerical oracle

`expected.json` is hand-worked arithmetic on the two recordings, independent of the canonical fold. The test reads the
real minute bodies through the lake writer and reader, folds them, and compares both with literal expectations and a
plain loop over the recorded bytes. All comparison prices are `Decimal`; atol = 0 and rtol = 0.

| Figure | Call 600 | Put 600 |
|---|---:|---:|
| Regular-session last close | 5.57 | 2.08 |
| Vendor daily VWAP | 4.4091 | 2.7747 |
| Regular close minus daily VWAP | 5.57 − 4.4091 = **1.1609** | 2.08 − 2.7747 = **−0.6947** |
| Vendor daily last close | 5.14 | 2.39 |
| Regular-session volume | 31,657 | 28,954 |
| Vendor daily volume | 31,899 | 30,078 |
| Tail volume | 31,899 − 31,657 = **242** | 30,078 − 28,954 = **1,124** |
| Tail bars | 389 − 377 = **12** | 400 − 386 = **14** |

The daily close and volume match the final close and summed volume of the complete minute recording. The exact price
deltas correspond to approximately +26.33% and −25.04% of the daily VWAP. Those rounded percentages are explanatory,
not test tolerances. This is `hand_computed` evidence for the fold and delta, with observed vendor market inputs.

## Regeneration

From `PythonDataService`, recover the preserved payloads without querying the vendor:

```sh
gh api repos/tim1016/learn-ai/issues/comments/6387331949 --jq .body > /tmp/3540-approved-vendor-comment.md
python tests/fixtures/golden/lake-studies/lake-options-spy-price-rule-2025-06-10/v1/regenerate.py /tmp/3540-approved-vendor-comment.md /tmp/3540-price-rule-recovered
```

The script requires an empty output folder, validates the contracts and unadjusted flag, preserves each JSON code
block with a trailing newline, and writes the source metadata and hand-worked expected values. It imports no
application code, opens no network connection, and cannot replace an existing fixture. The command was run to create
the four JSON files in this version. A new vendor recording requires new owner approval and a new fixture version.

## Scope

This proves the price-rule difference for two traded SPY contracts on one full session. It does not prove listing
pagination, another day's prices, live data availability, or the IV solver: these contracts have only three days to
expiry, below that solver's seven-day eligibility floor. Separate synthetic chain tests prove IV recovery and the
classified contract-selection changes.
