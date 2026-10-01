# ADR 0022 — Temporal authority: calendar owns scheduled structure, the live feed owns real-time liveness, one canonical calendar module

**Status:** Accepted 2026-07-05

## Context

Two temporal concerns were scattered and partially contradictory across the repo.

**1. Timestamp representation** already had a rule — `.claude/rules/numerical-rigor.md` § "Timestamp rigor" — mandating `int64 ms UTC` at every boundary. It is authoritative but violated in ~22 places: the .NET GraphQL surface ships `DateTime`/`string` for `AggregateBar.Timestamp`, `CreatedAt`, `Entry/ExitTimestamp`; the Angular GraphQL types therefore receive ISO strings while REST endpoints correctly use `_ms` numbers; chart code compensates with `new Date(iso).getTime()/1000`.

**2. Trading-calendar truth** had no rule at all, and "the calendar" was scattered across both obvious and less obvious places:
- `app/lean_sidecar/trading_calendar.py` — batch/backtest session structure.
- `app/engine/live/nyse_calendar.py` — live previous-session-close + session-state, whose docstring declares itself *intentionally separate* from the batch module ("different consumers, different test surfaces, different rate of change").
- Seven files hardcoding `time(9, 30)`/`time(16, 0)` (`spy_orb.py`, `operator_surface.py`, `polygon_ingest.py`, `tv_ingest.py`, `engine_runner.py`, `derived_daily.py`, `run_spy_partial_parity.py`), all of which silently mishandle early-close half-days.
- Additional direct `mcal.get_calendar("NYSE")` constructors (`chart_service.py`, `dataset_service.py`, `data_quality_service.py`, `volatility/basis.py`, `research/runs/window.py`) that bypass the would-be canonical module.
- Live market status is fetched from Polygon's real-time API (`market_monitor.py`), not from any calendar.

"The calendar is the source of truth" cannot be true while session hours live in nine places, and it is also *false at the live edge*: a static calendar computed from a holiday table physically cannot know about an unscheduled halt or an emergency early close. Forcing every "is the market open?" question through the calendar would make the live operator surface lie.

## Decision

**One temporal authority**, written as `.claude/rules/temporal-rigor.md` (the timestamp policy is extracted out of `numerical-rigor.md` and joined with the calendar policy; a stub pointer remains). It governs two things and only two things — representation and scheduled structure — and draws an explicit boundary against a third.

**a. Representation.** Every temporal value in flight, at rest, or on the wire is `int64 ms UTC`. Values that are semantically a *date* or a *wall-clock session boundary* (option expiry, trading date, `09:30` open) are still one `int64 ms UTC` field, constructed at a **defined ET session anchor** (e.g. expiry = 16:00 ET of that date) so the instant is unambiguous. There is no second date type.

**b. Scheduled structure — calendar is the sole source of truth.** Trading days, session open/close, early closes, bar alignment, warmup — all derive from **one canonical calendar module**. No hardcoded `time(9, 30)`/`time(16, 0)` anywhere. This is deterministic and reproducible for any instant, **past or future** (the calendar knows Christmas 2027 is closed).

**c. Real-time liveness is out of scope.** "Is the market open *this exact second*?" — which must reflect unscheduled halts — is answered by the live broker/vendor feed, not the calendar. The feed **must agree with the calendar on scheduled days** and may diverge only on real-time exceptions. When they diverge: the live signal wins for *operational* decisions, the calendar wins for *backtest reproducibility*. This is not a violation of (b) because it answers a question (b) does not govern.

**d. Hard collapse to one calendar module.** The "intentionally separate" split between `trading_calendar.py` and `nyse_calendar.py` is reversed. One canonical module owns all session math; `nyse_calendar.py`'s two functions fold in as additive helpers (`session_close_ms_utc`, `session_state_at_ms`, `previous_completed_session_close_ms`). The seven hardcoded sites are deleted and repointed. Any thin adapter that must survive for a concrete reason carries a parity test naming the canonical file, per ADR 0068 Decision 4 (reworded 2026-09-30 from a rule-file citation). Because the app is local-only and not production-deployed, the migration is destructive rip-and-replace with no compatibility shims.

**e. One frontend display component.** A shared, reusable Angular component renders an `int64 ms UTC` input in one of three declared modes: `local` (default, for instants), `et` (session/trading contexts), `date-et` (date-anchored values, never drifts a calendar day). The mode — not the viewer's zone — decides the display, which kills the option-expiry off-by-one-day bug.

## Consequences

- Market-open is deliberately computed in **two** places (calendar for scheduled, live feed for real-time). A future reader who "consolidates" them into one reintroduces either a lying live surface or an unreproducible backtest. This ADR is the reason not to.
- The reversal of the `nyse_calendar.py` "intentionally separate" decision is deliberate; one canonical implementation per concept (ADR 0068) outranks the prior separation rationale, and the two consumers are proven reconstructible from one enriched module.
- The destructive wire migration touches .NET DTOs, generated Angular types, and chart parsing simultaneously. It is reviewed as its own slice; the rule is authoritative immediately regardless.
- `pandas_market_calendars` becomes a single-point dependency for the canonical module and must be version-pinned (it is currently unpinned in `requirements-light.txt`).

## Alternatives considered

- **Calendar as the *sole* truth, rip out the live Polygon path.** Rejected: the live operator surface would be unable to reflect an unscheduled halt — a static holiday table cannot know real-time exceptions. Destructiveness does not fix an epistemic limit.
- **Split on historical-vs-future.** Rejected: the calendar authoritatively answers future *scheduled* questions (2027 holidays). The real seam is scheduled-structure vs unscheduled-real-time, not past vs future.
- **A second `date` type for date-only values.** Rejected: it breaks the single-representation invariant. Anchoring dates to a defined ET session instant keeps one field while removing the ambiguity.
- **Keep timestamp policy in `numerical-rigor.md`, add a separate `calendar.md`.** Rejected: timestamp and calendar are the same question ("what does this instant mean"); one reader, one file.

## Amendment 2026-09-30 — accepted deviations, the admissible range, finite-ingestion fail-fast, and the lake's trading-date anchor (#2749)

These rules ran under this ADR but were written down only in `.claude/rules/temporal-rigor.md`, in code comments and in owner rulings (#2745 entry 7, widened by #2746 E7+a and E7+b). This ADR, not the rule file, is now their record. Code comments re-point here in later clean-up slices. Decisions (a)–(e) are unchanged.

**f. Accepted deviations from (a).** Each one is named here:
- **Polygon news filter parameters** (`app/routers/news.py`). The five `published_utc*` *filters* stay vendor date strings. Polygon accepts a bare `YYYY-MM-DD` with whole-day semantics, which an instant cannot express, so canonicalizing them would narrow what a caller can ask for and gain nothing. Returned values are still canonicalized to `published_utc_ms` at ingestion (`PolygonClientService.list_news`), and the vendor string is not kept.
- **Data Lab session windows.** These are entered as `YYYY-MM-DD` strings (`DataLabSessionInput.FromDate` / `ToDate`, `Backend/GraphQL/DataLabMutation.cs`). The never-used `int64 ms` input branch (`WindowStartMsUtc` / `WindowEndMsUtc` / `CreatedMsUtc` / `UpdatedMsUtc` on the input) went in #2756, by owner ruling ☆ on map #2700 (2026-09-30), which chose this over the wire-format preference.
- **An owner-facing CSV may carry one display-only wall-clock column** (owner decision 2026-09-19, #2217). This is (e) applied to a file, not a new wire format.
  - `unix_ts` (`int64 ms UTC`) is always present, always first, and cannot be deselected.
  - The readable column is rendered server-side from `unix_ts` in one owner-chosen IANA zone, and named for that zone (`time_america_chicago`).
  - Nothing in the repo parses that column back, stores it or aligns on it.

  Surface: the Data Lab `dataset.csv` (`app/services/dataset_service.py::build_csv_bytes`). The options companion has no readable time column; its columns start at `unix_ts`.
- **The LEAN on-disk format** has its own record: ADR 0049 §1a.
- **The data lake's trading-date anchor is 12:00:00.000 UTC of the calendar date, not the ET session open** (#1877; `app/data_lake/types.py::trading_date_to_calendar_anchor_ms`, `Frontend/src/app/shared/data-lake/trading-range.ts::tradingDateToMs`). Backfill, ensure-data and coverage request windows carry it. Because it is pure UTC arithmetic, the wire value never depends on the browser's zone or on a DST boundary. Noon UTC is 07:00 or 08:00 ET, so it lies inside the intended ET day under either offset. `POST /ensure-data` and `POST /backfill` reject a value that is not exactly on the anchor. `GET /coverage` accepts any instant inside the ET day.

**g. The admissible range is `MAX_TIMESTAMP_MS`, not the int64 width.** The width is the storage type, not the bound. The latest admissible instant is `MAX_TIMESTAMP_MS = 253402300799999` (`app/utils/session_anchors.py`): the end of 9999-12-31 UTC, which is also the end of what `datetime` can represent. The rule: every `*_ms` boundary schema field declares that ceiling. The migration is done (#2771): every instant `*_ms` boundary field declares `le=MAX_TIMESTAMP_MS`. Durations are exempt, since a `*_ms` duration is not an instant. `le=2**63 - 1` is not a statement about the domain, and it is not representable in float64 either. Publishing it made the OpenAPI contract state a ceiling one higher than the one enforced (#1936). `2**63` still belongs in representability guards, which ask whether a value fits an int64 column.

**h. Finite ingestion fails fast and never repairs.** At conversion boundary 1, a finite vendor fetch is validated as it is ingested. That covers a historical bar window, a cache file and a set of Polygon chunks. Timestamps must be unique and strictly increasing. A duplicate or out-of-order timestamp is refused with a descriptive error, or its day is quarantined. Nothing calls `drop_duplicates`, forward-fills or reorders. Why: in a closed dataset, a duplicate or a gap is upstream corruption, and repairing it hides the signal. This governs:
- `app/services/sanitizer.py`;
- `app/services/bar_persistence.py`;
- `app/services/dataset_service.py` (`CanonicalBarsError`);
- `app/data_lake/bar_validation.py`;
- `app/lean_sidecar/polygon_canonical.py`;
- the IBKR finite history path (`app/broker/ibkr/bars.py`, `policy="strict"`).

**An active broker subscription is the one narrow exception.** A live stream may redeliver the element it sent last. Absorbing that redelivery idempotently, logged and counted and never silently, is ADR 0053 §14's decision together with its #2376 amendment (`policy="live_idempotent"`). It is not this decision, and absorbing a redelivery is not repairing a feed.
