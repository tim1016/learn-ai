# Position Sizing & Portfolio Allocation — Research Handoff

**Purpose.** This document audits how our strategies currently size positions and
reframes the open questions as **self-contained research briefs** you can paste
into a deep-research tool to find industry best practice. Each brief restates
its own context (the research tool has no access to this repo) and ends with
concrete questions.

**Status (2026-06-08):** Sizing is fused into each strategy's signal code, is
inconsistent across strategies, and there is no portfolio/capital-allocation
layer. This surfaced during the first live deployment-validation run: the bot
sized with `SetHoldings(SPY, 1.0)` and bought **336 shares (~$250k)** — the
entire paper account — on a single signal. That is correct for *one* strategy on
*one* account, but does not survive a second strategy.

---

## 1. What we do today (the inventory)

Engine algorithms live in
`PythonDataService/app/engine/strategy/algorithms/`. Each is a hand-coded
algorithm that emits orders directly.

| Strategy | Sizing call | Effective sizing |
|---|---|---|
| `spy_ema_crossover` | `SetHoldings(SPY, 1.0)` | 100% of equity (all-in) |
| `spy_orb` | `SetHoldings(SPY, 1.0)` | 100% of equity |
| `sma_crossover` | `set_holdings(symbol, 1.0)` | 100% of equity |
| `rsi_mean_reversion` | `set_holdings(symbol, 1.0)` | 100% of equity |
| `buy_and_hold` | `set_holdings(symbol, 1.0)` | 100% of equity |
| `deployment_validation` | `set_holdings(symbol, 1.0)` | 100% of equity |
| `spy_strategy_a/b/c` | inherit `_rsi_range_base` → `set_holdings(symbol, 1.0)` | 100% of equity |
| `spy_vwap_reversion` | `market_order(symbol, QUANTITY=100)` | fixed 100 shares |
| `spy_ema_crossover_options` | `market_order(..., contracts_per_trade)` | fixed N contracts |

**Problems this creates:**

1. **No coexistence.** Nine of the equity strategies target 100% of equity.
   Two of them on one account would each try to use the full buying power; one's
   fill changes the other's available margin. They cannot run together.
2. **No risk basis.** "100% of equity" and "100 shares" are both arbitrary —
   neither accounts for the instrument's volatility, the trade's stop distance,
   or how much of the account a single loss may cost.
3. **Sizing is hardcoded into the signal.** The algorithm decides *both* when to
   trade *and* how much, so we cannot change sizing without editing (and
   re-validating, including re-running the QuantConnect parity backtest) the
   strategy.
4. **No portfolio layer.** Nothing owns total equity, per-strategy capital
   budgets, aggregate gross/net exposure, or per-name limits.

**Relevant code, for our own reference (not for the research tool):**
- Strategy algorithms: `PythonDataService/app/engine/strategy/algorithms/*.py`
- Live portfolio (position/PnL tracking per run): `PythonDataService/app/engine/live/live_portfolio.py`
- Declarative spec (where a sizing policy field *could* live):
  `PythonDataService/app/engine/strategy/spec/`
- Frontend portfolio tracker: `Frontend/src/app/components/portfolio/`

---

## 2. Research briefs (paste these individually)

> Each brief is standalone. Split them into separate research calls — do not
> merge topics or time periods.

### Brief A — Position-sizing methods for a single systematic equity strategy

> I run a systematic intraday equity strategy (long-only, US large-cap like SPY,
> 1-minute to 15-minute bars, a few round trips per day, market orders). Right
> now it sizes by targeting 100% of account equity per trade, which is clearly
> naive. I want a rigorous survey of how professional systematic traders size a
> *single* strategy's positions.
>
> Cover, with the math and the trade-offs of each: (1) fixed share quantity;
> (2) fixed dollar notional per trade; (3) fixed fraction of equity / target
> percent; (4) volatility-targeting (size so each position contributes a target
> annualized volatility, e.g. via ATR or realized vol); (5) fixed-fractional and
> fractional-Kelly (size from edge and odds); (6) risk-per-trade sizing (size so
> a stop-out costs a fixed % of equity given the stop distance). For each:
> when is it appropriate, what inputs it needs, how it behaves as the account
> grows or volatility regime changes, and the common failure modes (e.g. Kelly
> over-betting, vol-targeting in gaps). Cite practitioner and academic sources.

### Brief B — Running many strategies on one account (capital allocation)

> I want to run multiple independent systematic strategies on a single brokerage
> account at the same time. Each strategy currently assumes it owns the whole
> account, so they would fight for buying power and margin. How do professional
> shops allocate capital across strategies on one account?
>
> Cover: capital "sleeves"/budgets per strategy; notional vs margin-based
> budgeting; how to handle shared buying power and the fact that one strategy's
> fill changes another's available margin in real time; aggregate gross/net
> exposure limits and per-name caps that span strategies; netting offsetting
> positions in the same instrument; correlation-aware allocation (risk parity,
> mean-variance, hierarchical risk parity) vs simple equal-weight; and rebalance
> cadence. Explain where the "meta-portfolio" allocation logic typically lives
> relative to the strategies themselves. Cite sources (multi-strategy/portfolio
> construction literature, practitioner write-ups).

### Brief C — Architecture: separating signal, sizing, allocation, and execution

> In my engine, each strategy's code decides both the trade signal and the order
> size in the same place. I suspect best practice is to separate these concerns.
> Describe the standard architecture used by mature systematic-trading frameworks
> to split: (1) signal/alpha generation, (2) portfolio construction / position
> sizing, (3) risk management / limits, (4) execution. Use QuantConnect LEAN's
> Algorithm Framework (Alpha Model → Portfolio Construction Model → Risk
> Management Model → Execution Model) as one concrete reference, and contrast it
> with how Zipline, backtrader, and at least one institutional setup organize the
> same responsibilities. I especially want to know: what interface/data crosses
> each boundary (e.g. an alpha emits an "insight"/target weight, the portfolio
> model turns weights into target quantities), and how to make sizing a
> declarative configuration rather than imperative code inside the strategy.

### Brief D — Sizing a deployment-validation / canary bot

> Separately from alpha strategies, I run a tiny "deployment-validation" bot
> whose only job is to exercise the live order → fill → reconcile → flatten path
> against a paper brokerage account (not to make money). It currently sizes with
> target-100%-of-equity, which buys ~$250k of stock just to test the plumbing,
> and large market orders split into multiple partial fills which complicates
> reconciliation. What is best practice for sizing a canary/smoke-test trading
> bot? Should it use a fixed 1-share (or minimal fixed) quantity? How do teams
> validate live trading infrastructure cheaply and deterministically while still
> placing real orders, and what edge cases (partial fills, min order size, lot
> rounding, commissions) should the validation deliberately still exercise?

### Brief E — Risk overlays and limits on top of sizing

> Once per-strategy sizing and cross-strategy allocation exist, what risk
> overlays do professional systematic shops layer on top? Cover: max gross and
> net exposure, per-instrument and per-sector caps, max position concentration,
> leverage/margin limits, intraday vs overnight exposure rules, drawdown-based
> de-risking (cutting size after losses), volatility-regime scaling, and
> kill-switches. Explain where these checks sit relative to sizing and execution
> (pre-trade vs post-trade), and how they interact with a sizing policy without
> double-counting. Cite practitioner risk-management sources.

---

## 3. Resolved plan (2026-06-08)

> This section was rewritten after a `grill-with-docs` session walked the full
> decision tree. The architecture is recorded in **ADR 0009 —
> `docs/architecture/adrs/0009-live-sizing-authority-and-provenance.md`** and the
> operator vocabulary in **`CONTEXT.md` § "Sizing authority"**. The earlier
> guesses (sizing as a *spec* field; a capital-allocation layer up front;
> deployment_validation needing a fresh QC anchor) were **superseded** — see the
> notes below. Load-bearing code claims were adversarially verified before this
> rewrite.

### The shape, in one breath

Live sizing is a **`live_config.sizing` policy** (canonical, hashed into `run_id`),
**not** a spec field — because the live runtime executes hand-coded algorithms,
not the spec. Python resolves the quantity; Angular only selects the policy. Two
**engine-derived** ledger stamps record `governed_by` (live_config vs
strategy_explicit) and `sizing_provenance` (reference_native vs live_override,
fail-closed). The policy intercepts **`set_holdings` only** (explicit
`market_order`/contracts win); a thin **`order_sizer.py`** adapter delegates the
percent path to the existing `LeanSetHoldingsSizing` quantity-math authority.
**`FixedShares(1)` is the global deploy default** — all-in is opt-in via a gated
**Reference parity** preset. See ADR 0009 for the four kinds, the preset gate, and
the deferred capital-sleeve seam.

### Three corrections to the old § 3

1. **Sizing lives in `live_config`, not the spec.** A declarative `SizeRule`
   already exists in the spec but the live path never runs it; a second vocabulary
   would violate single-source-of-truth. (ADR 0009 Decision 1–2.)
2. **The capital-allocation layer is deferred, not up front.** The `FixedShares(1)`
   default dissolves the coexistence failure for the common case; only two all-in
   bots collide, guarded interim by a pre-flight refusal. The future *capital
   sleeve* slots in at the portfolio-value provider feeding the percent path.
   (ADR 0009 Decision 9.)
3. **The `deployment_validation` → 1-share switch is config-only — no QC re-cut.**
   The algorithm is unchanged (`set_holdings(…, 1.0)`); the policy reinterprets the
   magnitude at the interception boundary, so `code_sha`/spec/QC anchor are
   untouched and only `run_id` changes (stamped `live_override`). This supersedes
   the "changing sizing changes the algorithm" note above. (ADR 0009 Decision 8.)

### v1 PR sequence

Everything below is **net-new** (none of `live_config.sizing`, `FixedShares`,
`order_sizer.py`, `sizing_surface`, `StrategyExplicit` exists today).

1. **Engine core.** `engine/execution/order_sizer.py` (adapter above the single
   `SizingModel` authority) + the Pydantic sizing union + wire
   `LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` into the live
   path (`LiveEngine`/`LivePortfolio` gain a sizing injection point) + a
   **pass-through portfolio-value provider** (sleeve drop-in later). Retire
   `SimpleFloorSizing` from live. **Pin the intentional `SimpleFloor → Lean`
   share-count shift with a regression test + a `docs/references/` note.**
2. **Deploy plumbing + audit.** Extend the `_live_config_from_ledger` allow-list
   (`run.py:540`) narrowly for `sizing`; validate the sizing union at both
   boundaries (`HostRunnerDeployRequest.live_config` + run start); thread the
   resolved policy through; stamp `governed_by` / `sizing_provenance`; add the
   `qc_audit_copy_sha256 → rule` allow-list; **fail-fast on the first
   order-surface mismatch.**
3. **Registry.** Add `StrategyRegistration.sizing_surface: "policy" | "explicit"`
   (`routers/engine.py:341`); mark `spy_vwap_reversion` + `spy_ema_crossover_options`
   `explicit`; surface it via `StrategyInfo` **and widen the trimmed
   `EngineStrategyInfo`** the deploy form reads (`live-runs.types.ts:272`,
   `live-runs.service.ts:214`) — the deploy form does *not* read the full
   `StrategyInfo`.
4. **Deploy form.** Sizing-preset control (Safe canary default / Reference parity
   gated / custom `FixedShares`/`FixedNotional`); disabled + "self-sized" label for
   `explicit` strategies; surface the Reference-parity gate state. This makes
   `FixedShares(1)` the default deploy and fixes the `deployment_validation` canary
   with no code/QC change.
5. **Interim all-in coexistence guard.** Pre-flight refusal when resolved sizing is
   `SetHoldings(1.0)` and the account is non-flat or another managed all-in bot is
   active. `FixedShares`/`FixedNotional` never blocked.

### Deferred (not v1)

Cross-strategy **capital sleeve** layer (seam named: portfolio-value provider);
**risk overlays** (Brief E); **advanced sizing** — risk-per-trade, ATR,
vol-targeting, fractional-Kelly (Brief A) — until strategies declare stop/ATR/edge
inputs; arbitrary fractional `SetHoldings(0<f<1)`; spec → live execution migration
(which would make `spec.entry.size` canonical-for-live).
