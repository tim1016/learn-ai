# SPY-DAILY-001 — the vendor's real daily bars for SPY, 2025-01-13 to 17, against the Cross-sectional study's daily fold

Receipt for #3524 (the Cross-sectional study reads its stock bars from the data lake). The study folds the lake's one-minute
bars into one daily bar per New York session. The owner chose the regular session (09:30 up to, not including, 16:00) and asked
that the fold be checked against the vendor's own daily bar. This fixture is that check: the vendor's real answer, kept byte for
byte, and what the fold differs by.

## Source

Polygon (now Massive) REST API, `https://api.polygon.io`, one `GET`, recorded on **2026-10-24** with the owner's approval (the
owner approved three Polygon calls that day):

`/v2/aggs/ticker/SPY/range/1/day/2025-01-13/2025-01-17?adjusted=false&sort=asc&limit=50000`

HTTP 200, `status: "OK"`, `resultsCount: 5`, `adjusted: false`. The key rode in an `Authorization: Bearer` header and is in no URL;
the recording script refused to save an answer that contained it. `input.json` is the body exactly as the vendor sent it (682
bytes), never re-saved through a JSON library.

The minute side is the repository's own capture of the same week,
`tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/bars.json` (4,062 one-minute bars, extended hours included,
`adjusted=false`; its own attribution is beside it).

## What `output.json` is

For each of the five sessions: the vendor's open, high, low, close and volume; the regular-session fold of the capture's minutes
(`regular_fold`); the fold's close less the vendor's, as an exact decimal; the fold's volume over the vendor's; and the close
had the minute stamped 16:00 been folded in, with its difference. It is written by `regenerate.py measure` from `input.json`
and the capture, with no network and no application code, so the fold it measures is written out independently of the study's.
`tests/research/test_cross_sectional_lake_vs_provider.py::test_the_regular_fold_of_a_real_spy_week_equals_the_vendors_real_daily_open_high_and_low`
runs the study's own fold (the real lake path) against `input.json` and holds it to these numbers.

## Regeneration

```
POLYGON_API_KEY=<key> python tests/fixtures/golden/market-data/SPY-DAILY-001/v1/regenerate.py fetch <an empty folder>
python tests/fixtures/golden/market-data/SPY-DAILY-001/v1/regenerate.py measure input.json tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/bars.json output.json
```

`measure` is offline and was run to write `output.json`. `fetch` is a tidied version of the one-off script that made the recording
and **has not been run in this form**; the recording was made on 2026-10-24 by that script, with the same path, query and
header. Regenerating makes a **new** recording, not these bytes again: the vendor's `request_id` differs on every call and its
data for a past day can be revised. If it is, the pinned numbers in the test are re-measured and the change is made as a new
fixture version, with the reason in the commit.

## Parameters and assumptions

- **Unadjusted.** `adjusted=false`. SPY had no split in 2025, so the split-adjusted tree the lake reads holds the same prices.
- **The daily bar's `t` is 00:00 New York** of its session (`1736744400000` is 2025-01-13 05:00 UTC). The study stamps a daily
  bar with the session open instead; both name the same UTC date.
- **A minute bar's stamp is its START.** The minute stamped 15:59 holds 15:59:00 to 15:59:59 and is the last regular minute; the
  minute stamped 16:00 holds the sixty seconds from 16:00:00 and is not in the regular session.
- **Open, high, low are exact on all five days** for the regular fold. Folding in the minute stamped 16:00 also leaves them exact
  (the minute does not set the day's extremes on these five days).
- **Close.** The fold's close is the 15:59 minute's close. It equals the vendor's daily close on two days (2025-01-14 and 15) and
  is 0.15, 0.06 and -0.01 above it on the others. Folding in the 16:00 minute instead makes it equal on one day (2025-01-16) and
  off by up to 0.41. The owner's reading on #3524 is that the vendor's close is the official closing print; this fixture shows
  only that it is neither minute's close, not what it is.
- **Volume.** The regular fold holds 71.5 to 84.7 percent of the vendor's daily volume on these five days. The vendor's volume is
  larger than any fold of its minute bars; the owner's reading is that it counts trades the minute aggregates omit. This fixture
  does not establish why. The Cross-sectional study reads no stock volume.
- **One symbol, one week.** Not shown: other symbols (less liquid ones may differ more), a half day, a DST week, a split-adjusted
  window, a window with a missing minute.

## Reference kind

By the README's taxonomy this is **`vendor_observed`**: the oracle is the vendor's own number and its method is opaque, so the
fixture is not externally certified. It records an accepted divergence (the close and the volume) and pins it so that a change
to the fold, or to the lake's reading of the minutes, shows up as a changed number.

## Tolerance

Open, high, low: exact (`atol=0`, `rtol=0`). The close difference is pinned at `atol=1e-9`, `rtol=0` (a difference of two prices
on the lake's 1/10,000 grid; the float noise of the subtraction is far below it). The volume share is pinned at `atol=1e-12`,
`rtol=0` (one division of two exact sums).
