# Bots triage pane — Trader lens & price tape (design + build note)

**Status:** Merged (PR #1748) · reviewed & hardened, see §10 · **Date:** 2026-08-24 · **Type:** UX / Frontend (`v2-panel`)

Mockup this was built against (static, seeded values, published from the design review):
https://claude.ai/code/artifact/7e00071a-afd6-4fa8-82a2-d276c5ac3589

---

## 1. Problem

`/brokers/:broker/accounts/:id/bots` showed, for a bot that was simply idle:

- a **blank centre pane** — the widest region on the screen carried nothing;
- **seven stacked action buttons**, six of them disabled, each with a red heading, so unavailability rendered as alarm;
- the same blocked-command reasons **repeated verbatim** lower down in the "Command availability" card;
- copy in the internal closed vocabulary (`Durable Clerk state`, `Sealed Account Mismatch`, `attributed exposure`, `custody snapshot`) as the screen's *default and only* view — including affirmations of absent problems such as
  `No Execution Coverage Conflict → No coverage resolution is required.`

The per-bot panel route (`/bots/:sid`) had already solved the audience problem with a Trader/Operator lens split; the list page had no such split, so every visitor landed in the operator's vocabulary.

## 2. Key decisions

| # | Decision |
|---|---|
| D1 | **The triage pane gets the same two lenses as the panel route**, with the same names. Trader is the default. |
| D2 | **Trader shows** the price tape, where the bot stands, and what it decided. **Operator keeps** per-command availability and the custody journal — unchanged. |
| D3 | **One primary command**, taken from the backend-selected `primary_action_by_lens.trader` (issue #1665 / ADR 0027) — the same selection the Trader banner on the panel route uses, so the two screens can never disagree about a bot's headline command. |
| D4 | **At most one runnable secondary command**, in backend-presented order; every remaining action folds into the existing `BotBannerOverflowComponent`. This is a *cut*, not a re-ranking: no frontend policy decides which command matters. |
| D5 | **The tape is a compact canvas over `gallery/lib/candle-renderer`**, not `DualPaneChartComponent`. |
| D6 | **The tape reads `chart/live` on a debounce and a poll — never a stream.** |
| D7 | **The tape derives no numbers** (no last price, no session change) from its bars. *(Enforced in review via `showLastPriceTag` — §10 R3; the shared renderer had still been drawing the last-price tag.)* |
| D8 | **The copy rewrite is deliberately out of this change.** See §5. |

## 3. Why not `DualPaneChartComponent` (D5)

That component carries the live/Polygon source switcher, the indicator rail, fullscreen, and a lightweight-charts instance per mount. It is right for a route that owns one bot and an SSE stream. This pane is re-pointed at a different bot every time an operator moves in a 25-row rail, so it needs a renderer it can throw away cheaply.

`gallery/lib/candle-renderer` is already a pure, Angular-free canvas renderer with no per-mount chart instance, and its own module docstring anticipates exactly this reuse. `TriageTapeComponent` owns the DOM canvas, the `ResizeObserver`, and the interval control; the renderer owns math and pixels.

## 4. Why debounce-and-poll, not a stream (D6)

`BotTriageDetailComponent`'s previous docstring recorded the reason a chart had been kept off this pane: it would duplicate the panel route's streaming plumbing. That objection is answered rather than overruled —

- **Debounced** (`TAPE_DEBOUNCE_MS = 350`): arrowing down the rail would otherwise open and immediately abandon one chart request per keystroke.
- **Parked off-lens**: `tapeSelection` returns `undefined` unless the Trader lens is showing, so the Operator lens costs zero chart reads.
- **Polled with the panel** (15 s, `visibilityState === 'visible'`), not streamed, and defaulting to the coarse `1m` resolution rather than `5s`.
- **Evidence is still never polled.** The audit-log reasoning in the constructor is unchanged: `read_evidence_page` appends an `EvidenceAuditEntry` per call, so evidence is read only on a genuine operator act.

## 5. What this change deliberately does NOT do

The strings themselves are unchanged. `mission_verdict.explanation`, the readiness `explanation`/`cure` text, and the gate headlines are **server-authored copy** (`vocabulary.py`, `run_admission.py`, `recovery_policy.py`) under a copy-coverage contract test and a TS parity snapshot — decision #7's "no open-ended string reaches the UI unlabeled". Rewriting them in place would edit safety-critical admission and recovery policy modules for a presentation reason, and would take the Operator lens's precision with it.

The proposed follow-up is **additive**, and belongs in its own change:

1. Add an optional `trader_explanation` (and `trader_next_action`) to the mission-verdict and readiness projections, authored in `panel_projection_service`.
2. Author plain-language copy per verdict state — e.g.
   `Durable Clerk state has no active hold or unresolved uncertainty in this scope.` → **"Stopped — nothing open."**;
   `The immutable run binding names a different custody account than this Clerk snapshot.` → **"This bot was created under a different Alpaca account than the one you're viewing."**
3. Suppress readiness rows that affirm the absence of a problem (the `No … Conflict → No … is required.` pairs) from the Trader lens entirely.
4. Extend the copy-coverage contract test to the new fields; regenerate `broker.types.ts` via `npm run codegen:openapi`.

The Operator lens keeps the exact existing wording in all four steps.

## 6. Files

| File | Change |
|---|---|
| `bot-triage-detail/triage-tape.component.{ts,html,scss}` | New. Canvas tape + interval control + empty/loading/failed states. |
| `bot-triage-detail/bot-triage-detail.component.ts` | Lens signal, debounced+parked `chart/live` resource, primary/secondary/overflow action split, metrics relabelled for the standing list. |
| `bot-triage-detail/bot-triage-detail.component.html` | Lens bar; trader layout (tape + standing + activity); operator layout (metric tiles + evidence). |
| `bot-triage-detail/bot-triage-detail.component.scss` | Lens bar, trader grid, standing rows; tape height clamped (§7). |
| `bot-triage-detail/bot-triage-detail.component.spec.ts` | `getLiveChart` stub, `showOperator()` helper, 4 new tests. |

Review fixes (§10) additionally touched:

| File | Change |
|---|---|
| `gallery/lib/candle-renderer.{ts,spec.ts}` | R3: `showLastPriceTag` config field (default `true`, gallery unchanged) + unit test for the suppression. |
| `bot-triage-detail/triage-tape.component.{ts,html,scss}` | R3/R4: `showLastPriceTag: false`; `source` + `notices` inputs; Polygon palette + honest notice chip. |
| `bot-triage-detail/bot-triage-detail.component.{ts,html}` | R1: roving-tab keyboard nav + `role="tabpanel"`. R2: `journalSelection` gates the evidence read on the Operator lens. R4: threads dominant `source` + notices into the tape. |
| `bots-list-page/bots-list-page.component.spec.ts` | R2: the audit-honesty guard test opens the Operator lens for its baseline evidence read. |

## 7. One bug worth recording

The tape first shipped with `flex: 1` inside a scrolling column. The rail beside it (a decision list) set the grid row height, so the canvas stretched to **2181 px** and the candles fell below the fold — the `ResizeObserver` was working exactly as written. The tape is now *sized*, not stretched: `height: clamp(20rem, 46vh, 34rem)` with `align-items: start` on the grid.

## 8. Verification

- `ng test` — 237 files / 1892 tests pass, including 4 new tests: lens default, debounce-before-fetch, no-fetch-on-operator-lens, and one-command-plus-overflow.
- `npm run test:guards` — proxy-control and chart-timestamp guards pass.
- `eslint src/app/components/broker/v2-panel/bot-triage-detail --max-warnings 0` — clean.
- Manual: `localhost:4200`, Alpaca paper account `PA3KWXU1C4C3`, both lenses, 1m and 5s.
- **Post-review (§10):** 237 files / **1896 tests** pass (4 review-fix tests added); project-scope `eslint` clean; `tsc --noEmit` clean; chart-timestamp guard ok. All 22 CI checks green before merge.

## 9. References

- Mockup: https://claude.ai/code/artifact/7e00071a-afd6-4fa8-82a2-d276c5ac3589
- Lens architecture: `panel-shell/bot-panel-shell.component.ts` §"Lens architecture (S3 trader + S4 operator)"
- Backend-owned primary action: `bot-detail-banner/lifecycle-action.ts`, issue #1665, ADR 0027
- Canvas renderer: `gallery/lib/candle-renderer.ts`, `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` §3.1/§3.2
- Closed operator vocabulary: `PythonDataService/app/broker/v2panel/vocabulary.py` (decision #7)

## 10. Review fixes (shipped in the same PR)

Five review findings (Codex, CodeRabbit) were genuine gaps — not deferred scope — and shipped in commit `c47aa919` before merge. One finding was declined with reasoning.

| # | Finding | Fix | Authority |
|---|---|---|---|
| R1 | The lens tablist had no keyboard path to the inactive lens — only the active tab was tabbable, and no arrow-key handling. | `onLensKeydown` implements the roving-tab pattern (Arrow/Home/End move selection **and** focus), mirroring `DualPaneChartComponent`'s source switcher; each tab gains an id + `aria-controls`, and the lens content is a `role="tabpanel"` labelled by the active tab. | WCAG AA · `angular.md` a11y |
| R2 | The audit-logged evidence read fired on Trader-default selection, so `evidence_audit.jsonl` recorded a read the operator never saw — and a later Operator switch showed a page fetched at the wrong time. | The journal resource is parked on a `journalSelection` computed that returns `undefined` unless the Operator lens is showing; evidence is read only when Operator is opened, retried, or acted on. The 15 s poll still never touches evidence. | Audit honesty — `read_evidence_page` append-only log |
| R3 | D7 was not actually enforced: the shared renderer's `draw()` unconditionally drew `drawLastPriceTag`, which reads `bars[last].close` and compares `bars[0].open` to derive a session direction — a client-side price/direction the tape had promised not to derive. | `CandleRendererConfig` gains `showLastPriceTag` (default `true`, gallery tile unchanged); the tape sets it `false`, so the tag path never runs and D7 becomes literally true. | D7 · CLAUDE.md #5 (single numerical authority) |
| R4 | The tape passed only bars/markers to the renderer, dropping `ChartBar.source` and `ChartLiveResponse.overlay_notices`, so Polygon/mixed fallback bars looked identical to live IBKR. | The tape now takes `source` + `notices` inputs: notices render as an honest chip and Polygon bars get the distinct `sourceColors` palette from `DualPaneChartComponent`; `bot-triage-detail` threads a dominant-source reduction (matching dual-pane's `liveSource`). | ADR 0032 §2 |
| R5 | `fakeLiveChart()` was untyped, so contract drift would pass silently. | Typed against the generated `ChartLiveResponse`; typing it surfaced four missing fields (`as_of_ms`, `strategy_instance_id`, `symbol`, `trading_date_close_ms`) and a wrong `trading_date_open_ms: null`. | Contract-drift guard |

**Declined — split the lens branches into child components** (CodeRabbit; template > ~80 lines). The two branches already delegate to focused children (`app-triage-tape` + `app-triage-activity`; `app-triage-evidence`); wrapping each in a lens container would create shallow forwarders threading ~12 inputs with no logic of their own — prop-drilling that reads worse under this repo's deep-module guidance. The ~80-line figure is a guideline and the excess here is declarative `@if`/binding markup, not branching complexity. The tabpanel refactor (R1) tightened the structure without adding a shallow layer.

New tests: keyboard lens switching, the evidence gate, the fallback notice, and a renderer unit test for `showLastPriceTag`. The `bots-list-page` "does not refresh another bot's audited evidence" guard test now opens the Operator lens to establish its baseline read.
