# Final-bar decisions: how many backtest trades, and how the backtest should model them (#2467)

**Status:** research note, 2026-09-29, revised the same day after an independent review. Code examined at `8e138f73` (master, after #2440 merged as `d4c521b2`). Parent #2439; owner decision #2431; live change #2440; live bug #2596.

**Superseded by #2607 (2026-09-29).** The owner chose a different model from this note's recommendation: neither live nor the backtest acts on a decision taken on the closing bar. An ENTER is skipped, and an EXIT stays due for the program to decide again from the next session; nothing is priced after the close. The rule is one predicate, `app/lean_sidecar/closing_bar.py`. The numbers below describe the code as it stood at `8e138f73`.

## The answer

**First, a live-money bug this research found: live does not reliably refuse an entry decided on the last bar.** Nothing on the ENTER path compares the decision instant with the session close. The gate trusts the broker clock's OPEN flag, which is polled once a second and trusted for 5 s, and it drops the `next_close_ms` the clock reported. A 16:00 decision lands about 0.6 s after the close. Whenever the freshest clock reading was answered before 16:00:00, the ENTER passes, goes out as a market DAY order, and Alpaca fills it at the next open. That is filed as **#2596 (P1)**, and a fix is in progress.

For the EMA crossover program that runs on the clerk lanes, **7.2% of backtest trades on SPY are decided on the session's final bar** (6 of 83 over 590 sessions: 5 entries, 1 exit). The paper-only 30–70 RSI variant has 9.7% (54 of 559: 36 entries, 18 exits). Deployment Validation has none, because it stops deciding 15 minutes before the close.

The backtest fills those decisions at the final minute's close. Live decides that bar just after the close. Once #2596 lands, live refuses a final-bar ENTER and sends a final-bar EXIT as an after-hours limit (#2440).

The entries matter most. Skipping the five final-bar entries removes **17.56 of the backtest's 50.03 points per share (35%)** for the sealed EMA settings. That rests on 5 trades, 4 of them winners. For the variant it is 49%, over 36 entries. The exits matter far less. The first price an after-close order can get is the open of the first minute that starts after the decision. It sits **3.74 bps** from the close on average (median 2.80). The next open sits **42 bps** away.

**Recommendation:** model the two kinds differently.

- **ENTER on the final bar: skip it.** This models live correctly only once #2596 lands. Until then, live sometimes fills such an entry at the next open.
- **EXIT on the final bar: fill at the after-hours price.** Use the open of the first minute that starts after the decision, floored at the limit live sends. If no minute reaches the limit before after-hours ends, the position stays open.
- **Do not fill at the next open.** It is about 11× further from what live gets for an exit, and it credits entries live is meant to refuse.

Two other findings:

1. **Bar grouping matches.** Given the same complete one-minute bars, the bot's decision seam and the backtest engine produce identical decision traces on every bar held, final bars included.
2. **A broken parity check.** The check built to show that has compared nothing since March 2026, so no receipt ever showed the match.

Follow-ups: [#2607](https://github.com/tim1016/learn-ai/issues/2607) (the backtest model), [#2608](https://github.com/tim1016/learn-ai/issues/2608) (the parity check), [#2609](https://github.com/tim1016/learn-ai/issues/2609) (dead machinery). The entry race is #2596.

## Recommendation, with the numbers behind it

**1. A final-bar ENTER is skipped: discarded, not filled.**

- It is what live does once #2596 lands: refuse the ENTER, settle the staged candidate DISCARD.
- Until #2596 lands, live sometimes fills such an entry at the next open instead, so neither model matches live today.
- A next-open fill would credit trades live is meant to refuse. Such fills are 42 bps from the close on average, and up to 400.
- The skip removes 35% of the sealed EMA backtest's points (5 trades) and 49% of the variant's (36 trades). That is the largest single live-vs-backtest gap found here.

**2. A final-bar EXIT fills at the after-hours price.**

- The limit is `marketable_limit_price` at the decision close with the run's exit allowance.
- It fills on the first minute that starts after the decision and reaches the limit. The fill price is the better of the limit and the open of the first minute that starts after the decision.
- If nothing reaches the limit before after-hours ends (20:00, or 17:00 on a half-day, per `order_session_state_at_ms`), the position stays open into the next session.
- The proxy sits 3.74 bps from the close on average with no drift. At a 10 bps allowance or more the limit was reached on every session in the window.
- Filling at the limit itself would overstate the cost by the whole allowance, up to 55.8 points on the variant at 50 bps. Filling at the next open is wrong by 42 bps on average.

**3. The next-open path stays LEAN-only.** It reproduces LEAN's equity fill for parity runs, and only there.

**4. Where the model applies.**

- It is the default for every run outside the LEAN-compatibility profile: Engine Lab and Strategy Lab runs, grid search, walk-forward, and the grades built on them. It is not opt-in, because an opt-in model would leave those numbers crediting the 35% by default. Each run records which final-bar model it used.
- It applies under every fill mode outside that profile, including the decision-minute open proposed in #2599. The backtest emits the final bucket on the next session's first minute, so without this rule each mode fills the decision either at a close live cannot trade at, or on the next day.
- The LEAN-compatibility profile is exempt. It exists to match LEAN, which fills a stale signal at the next open.
- **Deploy's gate is a LEAN-parity proof.** Deploy admits a strategy on its validation record or a Golden validation case (`app/services/strategy_validation_admission.py`). A Golden case needs a LEAN companion run (`app/research/golden_validation/service.py:190-211`). A run only gets one under the LEAN-compatibility profile (`app/services/parity_companion.py:80`). So the evidence that gates Deploy fills a final-bar ENTER at the next open, and this model does not change what Deploy admits. Whether Deploy should also see the live-model result is an owner decision.

**5. The model needs extended-hours minutes for the fill only.** Decisions keep using regular-session bars. The lake already holds after-hours minutes for all 590 SPY sessions.

**6. Owner decisions the follow-up needs.**

- **Allowance.** Which exit allowance does a backtest use? Options: a run setting, refusing the run like Start does, or the account's sealed value.
- **Proxy.** The open of the first minute after the decision, floored at the limit (recommended)? Or the limit itself (the most conservative, which costs the whole allowance)?
- **Deploy.** Should Deploy show or require a result under the live final-bar model, beside the LEAN-parity proof?
