# ADR 0066: The Symbol Picker Offers the Listing Universe and Populates the Lake on Selection

Date: 2026-09-20

**Status:** Accepted 2026-09-20

**Vocabulary:** none owed — "membership" (what a picker menu offers) and "coverage" (what the lake holds) are defined in this ADR and in `SymbolCatalogService`/`EnsureCoverageService`; "held", "backfill" and "adjustment mode" keep the meanings the data-lake surfaces already gave them.

Supersedes: none. Refines ADR 0049 (the data lake is the market-data authority) and the
retained-market-data decision in ADR 0062; it changes what a picker *offers*, not what any
engine *reads*.
Contexts: issue #1960 (Ticker Explorer's label-map picker), the retirement of the slow IBKR
symbol-search proxy (2026-08-27 decommission), and the owner decision of 2026-09-20 that
pickers may offer any listed symbol provided the lake stays in the loop.

## Context

Every symbol picker used to be backed by the data-lake catalog: the menu offered exactly what
the lake held, because a backtest can only read bars the lake stores. That made the menu
honest but closed. A newly listed symbol was unpickable everywhere until an operator went to
the Data Lake Observatory and typed it into a free-text backfill form, and several surfaces
had grown bespoke free-text ticker inputs to escape that dead end — each one a place a typo
or an unlabelled symbol silently degraded the product (issue #1960's root cause was exactly
such an escape: a display-label map was doing membership duty).

The IBKR symbol-search proxy that once powered live lookups is retired; its 500 ms-pacing
shaped every workaround that survived it. The replacement listing source is the data-plane's
existing Polygon account: a reference-tickers walk surfaced as `GET /api/tickers/catalog`,
trimmed to the picker-row projection and TTL-cached.

## Decision

1. **One picker family, one icon renderer.** Every symbol input in the Frontend uses the
   shared instrument card (`app-instrument-card` and its picker wrappers) and renders rows
   through `app-asset-identity`. Free-text ticker inputs and hand-rolled suggestion lists
   are bugs, not shortcuts.

2. **The picker's universe is the listing catalog joined with lake coverage.**
   `SymbolCatalogService` offers every listed US-equity symbol, badge in hand: held span,
   "not held", or "delisted". The menu no longer pretends the lake is the world; the badge
   keeps it honest about what the world is.

   **The catalog is a market-reference surface, not a broker surface.** It is served by the
   data-plane core (`GET /api/tickers/catalog`, coordinator-mounted — the browser's ingress
   in the split fleet) from a cached Polygon reference-tickers walk. This keeps FR-041
   intact (the coordinator constructs no provider broker client — an earlier draft sourced
   the catalog from Alpaca's `/v2/assets` via the broker routers, which live only on private
   clerk agents and would have 404'd at the coordinator) and matches the owner's vendor
   boundary: listing membership is market reference data on the data-plane's existing
   Polygon account; tradability evidence stays with the order path.

3. **An unheld pick backfills first.** Selecting a not-held symbol runs the ensure-coverage
   gate inside the card: compose the spec from `backfill-defaults`, submit the standard
   data-lake backfill job, stream its progress into the dropdown, and only emit the selection
   once the lake catalog — re-read, not the job's word alone — confirms the bars landed.
   Populate, then use. The gate always requests the full allowed history (5 years, trade
   bars) so a symbol covered today cannot strand a narrower window chosen tomorrow.

4. **Vendor-only delisted symbols are opt-in.** The shared picker offers active listings plus
   any inactive symbol the lake already holds, because those bars remain real and the row is
   visibly badged "delisted". The Observatory's explicit "include delisted" toggle adds
   inactive vendor rows the lake does not yet hold. This keeps expanding a backfill universe
   with delisted names an operator's visible choice rather than a default that accretes by
   accident.

5. **A dark vendor catalog degrades visibly.** If the symbol-catalog read fails, pickers fall
   back to lake holdings under a "live catalog unavailable" banner with a retry — never a
   silent empty list, never a canned fallback. If a backfill fails, the strip says why and
   offers retry; nothing is selected on a false answer.

6. **Crypto and other non-equity classes are not offered** — the lake's `market='usa'`
   pipeline cannot backfill them, so offering them would promise what the gate can never
   deliver.

## Consequences

- The lake remains the sole market-data authority and the only thing any engine reads
  (ADR 0049 unchanged). Membership and data are now distinct roles: the listing catalog
  decides what the menu shows; the lake decides what a run can read.
- Order entry and other trading surfaces wait on a backfill for unheld symbols even though
  placing the order needs no bars. Accepted: one story everywhere beats a special case, and
  the Observatory panel remains the bulk/explicit path.
- The catalog read is on every picker page's critical path. Its TTL cache bounds the
  vendor traffic; its failure is a banner, not an outage of the picker.
- Backfill submission and the job's SSE fold are owned by one shared runner
  (`BackfillJobRunner`); the picker's ensure-coverage gate and the Observatory panel are
  both consumers projecting their own UI onto it, so the app holds one backfill state
  machine, not two.
- `TICKER_LABELS` loses its picker role permanently — it is display metadata, never
  membership.
- Picker hosts pass `adjustmentMode`; the coverage badge and the gate read the lake tree the
  host's run will actually read.
- **Open exceptions until the mop-up lands:** batch-runner's multi-symbol card still reads
  the lake-only catalog (no joined universe, no gate), and the unrouted Ticker Explorer
  still passes its `TICKER_LABELS` display map as a host universe (#1960). This ADR is the
  destination; those two surfaces are the documented debt, and the "every symbol input" and
  "joined universe" claims above bind the rest of the app from the moment this ADR is
  accepted.

## Enforcement

- The hard rule in `AGENTS.md` and the "Symbol picking" section of
  `.claude/rules/angular.md` bind both Codex and Claude sessions.
- `InstrumentCardComponent`'s spec pins the gate, the badge join, the degraded banner, and
  the host-universe escape hatch; `EnsureCoverageService`'s spec pins the populate-then-use
  contract through opaque per-request gate sessions (ownership by handle, never by
  matching the gate's fields), including the disarmed-gate guarantee (a cancelled or
  superseded backfill cannot re-open the strip or select a symbol) and the
  co-waiting guarantee (releasing one session never cancels a run another card waits on).
