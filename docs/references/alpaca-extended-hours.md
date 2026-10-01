# Alpaca extended hours — vendor facts and open risks

**Status:** the vendor facts and open risks behind ADR 0059 Decision 5. The
decisions and their reasons are ADR 0059 D5, its 2026-09-25 amendment and its
2026-09-30 amendment; the code is `marketable_limit.py`, `clerk/program_leg.py`
and `clerk/recovery_reduction.py`.

## Vendor facts (pinned 2026-09-08)

| Fact | Value | Source |
|---|---|---|
| Pre-market / after-hours | 04:00–09:30 ET / 16:00–20:00 ET, Mon–Fri | Alpaca "Orders at Alpaca" § Extended Hours Trading, `https://docs.alpaca.markets/docs/orders-at-alpaca` |
| Overnight | 20:00–04:00 ET, Sun–Fri (Blue Ocean ATS) — **not a decision phase in slice 3** | same |
| Order shape | `type=limit`, `time_in_force ∈ {day, gtc}`, `extended_hours=true`; anything else is rejected | same |
| Day order lifetime | eligible only on its day; unfilled after the close is cancelled; extended eligibility lets it execute in supported extended hours | same, Time in Force |
| Market order after 16:00 | queued for release the next trading day | same |
| Early-close days | not documented by Alpaca — the declared window applies unchanged; a venue cancel folds through D5.4 | plan ruling R2 |
| `/v2/calendar` `session_open` / `session_close` | legacy `0700` / `1900`, never explained (forum thread 2400, 2020–2023) — rejected as a source | `https://forum.alpaca.markets/t/calendar-what-are-session-open-and-session-close/2400` |

## The regular close: an EXIT sent after the session it was decided in (#2440)

The decision is ADR 0059 D5.4 and its 2026-09-25 amendment; the closing-bar
rule (#2607) is ADR 0045's. Vendor behavior it rests on:

- Alpaca's clock answer names the close it lasts until (`next_close_ms`), and the
  broker's close carries no margin: an answer read just before the close still
  says OPEN.
- An extended-hours limit is a DAY order, so Alpaca ends it at the after-hours
  close. Alpaca keeps a DAY extended-hours limit working through the regular
  session until that day's after-hours close.

## Operator flatten outside the regular session (#2007)

The decision and its reasons are ADR 0059's 2026-09-30 amendment (Decision 5).

Known edges and open risks:

- The re-drive's automatic price is computed, never operator-confirmed: it is exactly one sealed exit allowance through the live touch, inside the band by construction, but the operator did not see the quote it was priced against — only the reference quote and realized slippage reported after the fills.
- The spread gate trades a certain bad fill for overnight gap risk: refusing means the position carries. The cap is refused, never clamped — a mid-price invents a number nobody chose (ADR 0059 D4).
- `top_of_book`'s `observed_at_ms` is the poll time, not the last tick: a book that stopped ticking reads perpetually fresh, and no spread cap detects a frozen quote. Detecting one needs the feed to carry the last tick time — a code fix, tracked as a follow-up.
- The quote is IBKR's consolidated book; Alpaca executes extended-hours orders on its own venues, whose book can be thinner. A price that looks marketable here can fill worse, or not at all, there — unverified against a live extended-hours fill.
- IBKR bid/ask **size** units for US equities (shares vs round lots) are unverified live; the depth warning's threshold rests on them.
- Order-placing code bounds after-hours by the calendar on early-close days (#2440 review): `session_authority.order_session_state_at_ms` ends POST at the earlier of the declared close and the calendar's scheduled after-hours close (17:00 on 2026-11-27) and is CLOSED past it. A regular-hours bot's EXIT that reaches the broker after a 13:00 close is bounded by 17:00, an EXIT delayed to 17:30 is not sent, and an extended run's leg decided after 17:00 refuses `SESSION_CLOSED_AT_DECISION`. The declared window itself — which bars an extended run decides on, the shadow broker's session — is unchanged; #2391 moves the scheduled bounds into the canonical calendar. The vendor's early-close after-hours end is still unverified.
