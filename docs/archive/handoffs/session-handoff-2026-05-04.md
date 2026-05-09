> **Status:** Archived — session handoff from 2026-05-04, ephemeral operational record.
> **Do not use as implementation authority.**
> Current authority: `docs/ibkr-integration-authority.md`, `docs/ibkr-paper-deployment-plan.md`.
> Archived because: end-of-session context dump for the 2026-05-04 IBKR paper-trading work; authority docs supersede this.

# Session Handoff — IBKR Paper-Trading Live Runtime

**Context for a fresh agent or future session.** Read this top to bottom before doing anything; the conversation that produced these artifacts was long and the framing shifted twice.

---

## Goal

Stage-2 parity receipt: paper-trade `SpyEmaCrossoverAlgorithm` on IBKR via a new Python live runtime, reconcile trades against the same-window backtest, write the report. The user has a boss demo "tomorrow" (the day after this session, May 5 2026) and wants the strongest possible artifact ready by then.

The strategy is a bit-exact port of LEAN's C# `SpyEmaCrossoverAlgorithm` and lives at `PythonDataService/app/engine/strategy/algorithms/spy_ema_crossover.py`. It is **not** to be modified.

---

## Required reading (in order)

1. `docs/ibkr-paper-deployment-plan.md` — **v2 plan, the source of truth.** Post-Codex review. §0 lists the six load-bearing assumptions, all confirmed.
2. `docs/ibkr-paper-deployment-feedback.md` — Codex's critique that produced the v1→v2 deltas. Read this to understand *why* the v2 assumptions are what they are.
3. `docs/architecture/ibkr-integration-tdd.md` — the existing IBKR broker module (Phase 1+2+3 shipped, ~3,500 LOC, 86 tests). The plan layers on top of this; do not duplicate it.
4. `CLAUDE.md`
5. `.claude/rules/python.md`, `.claude/rules/numerical-rigor.md`, `.claude/rules/testing.md`
6. `docs/overnight-codex-prompt.md` — the unsupervised-overnight execution brief written for Codex CLI. **Note: not yet handed to Codex** (see "Open decision" below).

---

## State of the world at handoff

- **Plan v2 is finalized and reviewed.** No outstanding edits.
- **Pre-flight is clean.** From `PythonDataService/` with `.venv` active, ruff is clean and 100 tests pass on `main` (only dependency-deprecation warnings, nothing structural).
- **No implementation code has been written yet.** `app/engine/live/` does not exist. `app/broker/ibkr/bars.py` does not exist.
- **No overnight branch exists yet.** Suggested name: `overnight/ibkr-paper-runtime-20260504`.
- **Codex was not handed the overnight prompt.** The user paused before that step to question the agent/codex division of labor (see below).

---

## The open decision (this is what stalled the previous session)

The user questioned why the Cowork agent was authoring prompts for Codex to execute overnight, rather than doing the implementation directly.

**Capability split as it actually exists:**

- **Cowork agent (me)**: can read, write, edit files in the user's repo via Read/Write/Edit tools. Cannot run pytest against the user's `.venv`. Cannot `git commit` / `git push` directly.
- **Codex CLI** (user's terminal): can run pytest in the real venv, iterate on test failures, commit, push. The "more permissions" the user mentioned is real-environment access, not raw capability.

**Honest division of labor:** agent implements, Codex (or user) verifies. I had been treating myself as plan-author because that's how the user framed the first request ("a plan I can run through codex"), and I didn't push back when the framing outlived its purpose.

**Last proposal on the table** (user requested this handoff before answering):

- Cowork agent does **Phases 1-4** in the resumed session (scaffolding, `app/broker/ibkr/bars.py`, `LivePortfolio`, `LiveContext`) — spec-clearest, lowest-risk to write without iterative test feedback.
- Codex CLI does **Phases 5-7** overnight (asyncio engine driver, replay parity test, broker-event-collapse test) — these benefit from real test feedback that the agent can't provide.
- Phases 8-9 (config + CLI, reconciliation tooling) deferred to a daylight session with the user present.

**The first question to ask Inkant when resuming:** "Resume with that split, or a different plan? Time available in this session?"

---

## Phase scope (from plan §3-11)

| # | Phase | Files (new) | Notes |
|---|---|---|---|
| 1 | Adapter scaffolding | `app/engine/live/{__init__,live_context,live_portfolio,live_engine,config,run,reconcile,README}.py` plus matching empty test files | Pure scaffolding, no broker contact |
| 2 | Real-time minute-bar source | `app/broker/ibkr/bars.py`, new `IbkrMinuteBar` model in `app/broker/ibkr/models.py`, `tests/broker/ibkr/test_bars.py` | 5-sec TRADES bars aggregated to 1-min; useRTH=True; fail-fast on dup/non-monotonic timestamps; `int64 ms UTC` |
| 3 | `LivePortfolio` | `app/engine/live/live_portfolio.py`, `tests/engine/live/test_live_portfolio.py` | Delegates to existing `account.fetch_*` + `orders.place_paper_order` |
| 4 | `LiveContext` | `app/engine/live/live_context.py`, `tests/engine/live/test_live_context.py` | Adapter for `StrategyContext` surface; reuses existing `TradeBarConsolidator` |
| 5 | `LiveEngine` driver | `app/engine/live/live_engine.py` | asyncio loop with three concurrent consumers (bars, order events, force-flat scheduler); single-task strategy execution via `asyncio.Queue` |
| 6 | **Replay parity test (HARD GATE)** | `tests/engine/live/test_live_engine_replay.py`, `tests/engine/live/fixtures/fake_broker.py` | Exact match — `atol=Decimal("0")` — on order count, prices, quantities, fees, equity curve, trade log, insights, final state |
| 7 | **Broker-event-collapse test (HARD GATE)** | `tests/engine/live/test_live_engine_collapse.py` | Covers the failure mode Codex flagged: `stream_order_events` polling can collapse rapid status transitions |
| 8 | Paper config + CLI + hygiene | `config.py`, `run.py`, `paper.example.yaml`, `.gitignore` updates | Most safety delegated to existing `IbkrSettings`. **Deferred** to daylight session. |
| 9 | Reconciliation tooling | `reconcile.py` | Diffs paper vs same-window backtest, writes report under `docs/references/reconciliations/`. **Deferred** to daylight session. |
| 10 | Run paper week | (no code) | Human-driven; not an agent task |

---

## Decisions locked in v2

These are settled. Don't re-litigate without explicit user input:

1. Build Python `LiveEngine`, not LEAN-via-IBKR. Repo philosophy: sovereignty over the math.
2. **Reuse `app/broker/ibkr/` verbatim.** It's already shipped through Phase 3b with four-layer paper safety, idempotency, and SSE patterns. Adding a parallel client splits the safety boundary.
3. SPY-only, single symbol, paper port only in v1.
4. `FillMode.NEXT_BAR_OPEN` is the replay parity target; real broker fills are reconciled against it (cent-tolerance), not held to strict parity.
5. Hard paper-port guard already exists in `app/broker/ibkr/config.py`. Don't duplicate it in the new code.
6. Receipt runs at strategy-native `set_holdings(SPY, 1.0)`. No sizing cap. (Codex pushed back on the v1 0.10 cap because it changes integer share counts via `int(target_value/price)`, breaking parity rather than preserving it.)
7. Replay tolerance: **exact**. `atol=Decimal("0")`. Cent tolerance is for real-broker reconciliation only.
8. Insight scoring cadence in live: per-minute, matches backtest exactly.
9. `set_holdings` reference price in live: consolidated bar's close, matches backtest exactly. Plumbed through `LiveContext.set_holdings(symbol, fraction)` → `LivePortfolio.set_holdings(symbol, fraction, ref_price=consolidated_bar.close)`.
10. Order ID persistence: `.live_state.json` (atomic write, run-scoped path, gitignored). Postgres deferred — no migrations workflow exists yet.
11. Drop `mypy --strict` gate. The service has no `pyproject.toml`. Track as future work.

---

## Forbidden actions (do not do these even if it looks tempting)

- Modify `app/engine/strategy/algorithms/spy_ema_crossover.py`, `app/engine/engine.py`, or `app/engine/strategy/base.py`. The strategy class and `BacktestEngine` are unchanged by design; `StrategyContext` is the contract.
- Modify any file under `app/broker/ibkr/` except adding `bars.py` and one new model (`IbkrMinuteBar`) in `models.py`.
- Add a dependency to `requirements-light.txt` or `requirements-heavy.txt`. `ib_async>=2.0,<3.0` is already there.
- Use `# type: ignore`, bare `except:`, or `except Exception: pass`.
- Use ISO strings, naive `datetime`, or `pd.Timestamp` as wire/storage timestamp formats.
- Use `np.allclose` / `np.isclose` with default tolerances.
- Loosen the replay parity test's `atol=Decimal("0")` to make a test pass. The receipt is meaningless without exact match.
- Substitute `MagicMock` where the plan specifies a deterministic `FakeBroker`.

---

## Environment

```bash
cd PythonDataService
source .venv/bin/activate              # or .venv\Scripts\activate on Windows

# Pre-existing tests that must keep passing:
pytest tests/broker/ibkr/ \
       app/engine/tests/test_spy_validation.py \
       app/engine/tests/test_spy_next_bar_open_validation.py -x

# New live-runtime tests (will exist after Phase 1):
pytest tests/engine/live/ -x

# Project-scope lint:
ruff check app/ tests/
```

The user runs everything in `PythonDataService/.venv` on host — **not** inside `podman exec polygon-data-service`.

---

## Demo target

The replay parity test (Phase 6) passing exactly is the demo headline:

> "The live runtime produces identical trades to the backtest under controlled conditions. Real broker connection is operationally gated on our IBKR market-data subscription and is the next step."

Phase 7 (collapse test) is bonus polish that demonstrates the team thought about real-world failure modes a synchronous fake can't reproduce.

What to walk through with the boss: plan v2 (strategic narrative) → live test execution (proof) → roadmap to first paper trade (Phases 8/9 + operational items: market-data subscription, Gateway login, paper week).

---

## Conversation breadcrumbs

The session that produced these artifacts ran roughly:

1. Initial question: "how do I deploy my EMA crossover on IBKR for trading?"
2. Plan request → v1 plan written
3. Open questions handed to Codex for review (prompt drafted, user ran it externally)
4. Codex's critique returned, saved at `docs/ibkr-paper-deployment-feedback.md`
5. Critique incorporated → v2 plan
6. Execution-brief prompt drafted for Codex
7. Late-night pivot: user requested an unsupervised overnight execution plan
8. Pre-flight verified clean (100 tests passing, ruff clean)
9. User questioned why Codex was doing implementation instead of the Cowork agent
10. Cowork agent acknowledged the framing was wrong, proposed agent-implements / Codex-verifies split
11. User requested this handoff before authorizing the split

---

## First move when resuming

Ask Inkant:

1. Time budget for this session?
2. Proceed with agent-implements-1-4 / Codex-verifies-5-7 overnight, or different plan?
3. Demo time tomorrow — when, in their timezone?

If they confirm the split, start by creating the overnight branch, then begin Phase 1 (pure scaffolding — produces a viewable diff in <5 minutes).

If they want a different split, the most useful alternative is "agent does as much as possible, Codex picks up wherever the agent stopped."
