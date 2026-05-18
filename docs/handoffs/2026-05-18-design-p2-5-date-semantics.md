# Design handoff — P2.5 date-window semantics

**Use Claude (design) for this one** — it's an API-shape decision with
real downstream effects. The wrong choice locks in confusion across
the data-plane request shape, the manifest's persisted `parameters`,
the cross-engine reconciler's date-pairing logic, and the UI date
pickers. Don't let an implementation agent pick autonomously.

## What's broken (or at least muddled)

Date semantics today are inconsistent across the layers:

- **UI** (`Frontend/src/app/components/lean-lab/lean-lab.component.ts`
  line ~669) sends midnight-UTC values for `start_date` / `end_date`,
  derived from an HTML `<input type="date">`. The user picks a
  calendar date; the UI serializes as the UTC midnight ms.
- **Service**
  (`PythonDataService/app/services/lean_sidecar_service.py` line ~152)
  converts the request ms back to UTC calendar dates.
- **Staging**
  (`PythonDataService/app/services/lean_sidecar_service.py` line ~324)
  then iterates an inclusive weekday range and stages minute bars per
  NY-local trading date.
- **Manifest** persists `requested_window_ms` as the UTC ms verbatim
  and `staged_data_window_ms` as the ET-midnight envelope of the
  staged days.

The mismatch surfaces in three places:

1. **A one-day run cannot be represented cleanly.** Midnight UTC on
   Jan 6 = 7pm ET on Jan 5; the UTC ms range for "Jan 6 trading day"
   collides with the previous NY trading date at the boundary.
2. **`requested_window_ms` can disagree with `staged_data_window_ms`**
   in the manifest. The cross-engine reconciler then has to decide
   which one is canonical when pairing trades — it currently uses NY
   trading date derived from the bar's ms, which works but documents
   nothing.
3. **The reconciler relies on the bar-side NY conversion**, not the
   request-side, so the API's window semantics are effectively
   advisory.

Reviewer P2.5.

## Three viable approaches

### Approach A — API takes NY trading dates explicitly

Change the request shape from `start_ms_utc` / `end_ms_utc` to
`start_trading_date` / `end_trading_date` (ISO `YYYY-MM-DD` strings,
NY-local trading dates).

**Effort:** medium — request shape change, deprecate the ms fields,
update UI + tests + frontend type + manifest schema.
**Tradeoff:** semantically cleanest; explicit NY-trading-date is what
the reconciler actually uses. **Violates `numerical-rigor.md`'s
"every timestamp is int64 ms UTC" rule** — opens the door to
string-typed timestamps elsewhere.

### Approach B — API takes half-open session-boundary ms

Keep int64 ms UTC on the wire (preserves numerical-rigor.md's rule).
Tighten the contract: the request's `start_ms_utc` MUST be the NY
session-open of the first trading day (09:30 ET), and `end_ms_utc`
MUST be the NY session-close of the last trading day (16:00 ET).
Half-open: `[start, end)`. A one-day run is then
`(2025-01-06 09:30 ET, 2025-01-07 09:30 ET)`.

**Effort:** medium — keep types unchanged, change validators +
documentation + UI date picker (still picks calendar dates, but
serializes to session-open ms not midnight ms).
**Tradeoff:** keeps int64 ms UTC; pins the semantics; UI logic shifts
but the wire format doesn't. The most disruptive part is the manifest
— old runs persist midnight-UTC values that don't fit the new
contract.
**Recommended.**

### Approach C — Document the current midnight-UTC convention + add validators

Don't change the API. Add a validator that the request ms is exactly
NY-midnight (or UTC-midnight; pick one) and document the convention
in the request model docstring + ADR.

**Effort:** small — pure server-side normalization.
**Tradeoff:** doesn't fix the actual ambiguity; just documents what's
there. The cross-engine reconciler keeps doing its own NY-date
derivation downstream. Reviewer would likely flag this as
insufficient.

## Recommended: Approach B

Half-open session-boundary ms. Reasons:

- **Preserves `numerical-rigor.md`'s int64 ms UTC rule** — the most
  load-bearing constraint in this repo's design.
- **Removes the midnight-UTC ambiguity** without inventing a new
  type system.
- **Half-open `[start, end)`** matches the convention bars use
  internally (a bar ending at `09:31` has trades from `[09:30,
  09:31)`).
- **Migration cost is bounded:** the UI converts a `<input
  type="date">` selection to a session-open ms (`(date) →
  09:30:00 ET → UTC ms`); existing manifests stay readable (legacy
  midnight-UTC manifests still parse, just don't match the new
  invariant — flagged via a manifest schema_version bump).

## Implementation pointers (when an agent picks this up)

Files most likely to touch (NOT exhaustive — agent should grep):

- **Request shape:**
  - `PythonDataService/app/routers/lean_sidecar.py` —
    `TrustedRunRequestModel.start_ms_utc/end_ms_utc` validators get
    new semantics. Validator must reject midnight-UTC payloads with
    a clear error pointing at the new contract.
  - `Frontend/src/app/services/lean-sidecar.types.ts` — type
    documentation update (no shape change since it's still `number`).
- **UI conversion:**
  - `Frontend/src/app/components/lean-lab/lean-lab.component.ts`
    line ~669 — `isoDateToMsUtc` becomes
    `isoDateToSessionOpenMsUtc`. Needs the NY zone.
- **Staging:**
  - `PythonDataService/app/services/lean_sidecar_service.py` line
    ~152 — convert request session-boundary ms to NY trading dates
    explicitly. Weekday iteration drops the awkward `+1 day` because
    the session-boundary semantics are already inclusive-of-end.
- **Manifest:**
  - `PythonDataService/app/lean_sidecar/manifest.py` —
    `MANIFEST_SCHEMA_VERSION` bump (per D10 contract: bump on shape
    changes; the int64-type stays, the SEMANTIC of the field changed,
    which qualifies).
  - Add a note: legacy manifests (schema v<new>) carry
    midnight-UTC; new manifests carry session-boundary ms.
- **Cross-reconciler:**
  - `PythonDataService/app/lean_sidecar/cross_reconciler.py` — no
    change to the date-derivation logic (it already uses NY trading
    date from the bar's ms), but the manifest's
    `requested_window_ms` is now directly comparable.

## What this PR does NOT need to do

- **Don't change the wire types.** Keep `start_ms_utc` and
  `end_ms_utc` as `int`. The semantic shift is documented + enforced
  via validators.
- **Don't migrate old manifests.** Old runs stay readable on the
  legacy `schema_version`. Re-runs produce new-schema manifests.
- **Don't change `staged_data_window_ms` semantics** — it stays the
  ET-midnight envelope of staged trading days. The
  `requested_window_ms` is now semantically equivalent (session
  boundaries align with trading days), but the persistence of both
  fields is unchanged.

## Risks / things to watch

- **UI-side timezone library**: needs to compute "NY 09:30 of date X"
  for any given calendar date. JS `Date` is timezone-naive; use
  `Intl.DateTimeFormat` with `America/New_York` or import a small TZ
  helper. The frontend already has `zoneinfo`-style logic for
  rendering; check `lean-lab.component.ts` for the existing pattern.
- **DST transitions**: 09:30 ET maps to either UTC 14:30 (EDT) or
  UTC 13:30 (EST). The conversion MUST go through the NY zone — not
  via a fixed offset.
- **The Phase 5b reconciliation template** hardcodes `2025-01-06` to
  `2025-01-10`. Those tests need to keep passing after the manifest
  schema bump.

## Independence

This work has no upstream PR dependencies. It can land as a single PR
once the design is locked in. Probably ships with a follow-up doc
update to the ADR's "Date-window and bar-consumption" section.
