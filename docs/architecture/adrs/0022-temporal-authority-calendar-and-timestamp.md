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

**d. Hard collapse to one calendar module.** The "intentionally separate" split between `trading_calendar.py` and `nyse_calendar.py` is reversed. One canonical module owns all session math; `nyse_calendar.py`'s two functions fold in as additive helpers (`session_close_ms_utc`, `session_state_at_ms`, `previous_completed_session_close_ms`). The seven hardcoded sites are deleted and repointed. Any thin adapter that must survive for a concrete reason carries a parity test naming the canonical file, per CLAUDE.md guiding-philosophy #5. Because the app is local-only and not production-deployed, the migration is destructive rip-and-replace with no compatibility shims.

**e. One frontend display component.** A shared, reusable Angular component renders an `int64 ms UTC` input in one of three declared modes: `local` (default, for instants), `et` (session/trading contexts), `date-et` (date-anchored values, never drifts a calendar day). The mode — not the viewer's zone — decides the display, which kills the option-expiry off-by-one-day bug.

## Consequences

- Market-open is deliberately computed in **two** places (calendar for scheduled, live feed for real-time). A future reader who "consolidates" them into one reintroduces either a lying live surface or an unreproducible backtest. This ADR is the reason not to.
- The reversal of the `nyse_calendar.py` "intentionally separate" decision is deliberate; single-source-of-truth (CLAUDE.md #5) outranks the prior separation rationale, and the two consumers are proven reconstructible from one enriched module.
- The destructive wire migration touches .NET DTOs, generated Angular types, and chart parsing simultaneously. It is reviewed as its own slice; the rule is authoritative immediately regardless.
- `pandas_market_calendars` becomes a single-point dependency for the canonical module and must be version-pinned (it is currently unpinned in `requirements-light.txt`).

## Alternatives considered

- **Calendar as the *sole* truth, rip out the live Polygon path.** Rejected: the live operator surface would be unable to reflect an unscheduled halt — a static holiday table cannot know real-time exceptions. Destructiveness does not fix an epistemic limit.
- **Split on historical-vs-future.** Rejected: the calendar authoritatively answers future *scheduled* questions (2027 holidays). The real seam is scheduled-structure vs unscheduled-real-time, not past vs future.
- **A second `date` type for date-only values.** Rejected: it breaks the single-representation invariant. Anchoring dates to a defined ET session instant keeps one field while removing the ambiguity.
- **Keep timestamp policy in `numerical-rigor.md`, add a separate `calendar.md`.** Rejected: timestamp and calendar are the same question ("what does this instant mean"); one reader, one file.
