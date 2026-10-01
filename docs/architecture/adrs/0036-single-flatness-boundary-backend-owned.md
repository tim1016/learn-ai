# ADR 0036: One flatness boundary, owned by the backend

**Status:** Accepted

- **Date:** 2026-08-17
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1597](https://github.com/tim1016/learn-ai/issues/1597); the
  2026-08-17 numeric authority census (in Git history).
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-17.
- **Succeeds:** ADR 0013's "no frontend-derived verdicts" principle, which was
  marked *Superseded* when the IBKR Bot Control surface was removed and has had
  no live successor since. This ADR restores it for **numeric boundaries only**;
  it does not revive ADR 0013's wider operator-surface scope.
- **Vocabulary:** `CONTEXT.md` § "Flatness boundary (resolved 2026-08-17)".

## Decision

**1. There is exactly one rule for whether a quantity is exposure or flat.**

`PythonDataService/app/broker/alpaca/clerk/sqlite/folds.py::position_quantity_is_nonzero`
— `nonzero(q) = abs(q) >= POSITION_QTY_EPSILON` (`1e-9`, `rtol=0`) — is that rule.
Every site that classifies a quantity as exposure-or-flat calls it. There is no
second epsilon constant for this question, and no site may state the boundary
with the opposite inclusivity.

The alternative considered and rejected was naming two concepts — an exact-zero
rule for *summing our own execution effects* (where cancellation is exact and no
residue is possible) and a tolerant rule for *comparing our attributed quantity
against the broker's* (where residue is expected). That distinction is real, and
it is why the divergence below was easy to write. It was rejected because a
single stateable rule is cheaper to hold in the head and to enforce mechanically
than a correct-but-two-sided taxonomy, and because the cost of the rejected
alternative — one extra concept name — buys accuracy the product does not need
at `1e-9` share.

**2. The Frontend holds no flatness boundary.**

Angular does not decide whether a quantity is flat. Where a UI surface needs that
verdict it consumes a backend-authored one, arriving on the payload it already
reads; it does not test the number itself. This is the numeric case of ADR 0014's
backend-authored operator view, and it means the boundary in Decision 1 has
exactly one home in the system rather than one per stack.

## Scope

**In scope:** any classification of a share quantity as exposure or flat —
custody projections, reconciliation drift, exit resolution, recovery planning,
display caches, and UI guards.

**Out of scope:** `fifo_pnl.py`'s internal `_ZERO_ABS_TOL` where it decides *lot
exhaustion* ("is this FIFO lot consumed?"). That is a different question about a
different object, its arithmetic is parity-tested and correct, and folding it in
would touch working P&L code for no defect. It keeps its own constant. What it
may **not** do is lend that constant to an exposure decision — see below.

## Consequences

These follow from the decision and are **not** implemented by this ADR. Each
needs a regression test that fails before and passes after, per `CLAUDE.md`.

1. **`rollup_cache.py:169` is wrong today.** It prunes exposure with
   `abs(updated) <= _ZERO_ABS_TOL` — `fifo_pnl.py`'s *lot-exhaustion* constant,
   at the opposite inclusivity. At exactly `1e-9` the canonical says nonzero and
   the rollup says flat. It must call `position_quantity_is_nonzero`.

2. **`docs/references/clerk-position-drift-tolerance.md` currently states a
   falsehood.** Its "Reuse (#1379)" section promises that "exactly `1e-9` is
   never classified as both flat and nonzero by different workflows." Consequence
   1 is a live counter-example. The sentence becomes true when 1 lands; until
   then the doc overstates the guarantee and should say so.

3. **`journal_exposure.py::fold_execution_exposure` prunes exact zero
   (`quantity != 0.0`) and must conform.** This is a real behavior change: summed
   residues in `(0, 1e-9)` are currently retained as exposure and would become
   flat. Its golden fixtures must be reviewed rather than regenerated to pass —
   per `numerical-rigor.md`, regenerating a fixture to make a test pass is an
   anti-pattern.

   *Superseded 2026-08-27 by PR-C of #1813.* This consequence is no longer
   satisfiable as written: `app/engine/live/journal_exposure.py` was retired
   with the IBKR control plane, taking `fold_execution_exposure` and its
   `journal-exposure-projection` golden fixture with it — deleted alongside the
   code they proved, not regenerated. The flatness primitive that survives is
   `app/broker/alpaca/clerk/sqlite/folds.py::position_quantity_is_nonzero`,
   whose provenance block names it canonical; the conformance obligation
   this consequence created now attaches there. The original text above is left
   unedited as the historical record.

4. **`broker-deploy-form.component.ts:609` must stop testing the number.** It
   uses `Number(own.quantity) === 0` on broker-reported positions, which Alpaca
   may report fractionally, and blocks a Reference-parity deploy on any residue
   the backend would call flat. It errs toward blocking — safe in direction,
   wrong in fact. It consumes a backend verdict instead.

5. **Two Angular sites are *not* affected, and should not be "fixed".**
   `deploy-prefill-params.ts` rejects any non-`Number.isInteger` quantity before
   testing `!== 0`, so no residue can reach that comparison; its label helper
   reads the already-normalized record. Changing them would add a round-trip for
   no correctness gain.

6. **The math index's flatness row needed widening** (the index was cut in #2750).
   It recorded that "Angular
   renders the Python-authored plan and performs no closing-quantity
   calculation" — true, but narrower than Decision 2, which bars Angular from
   *any* flatness classification, not only closing quantities.

## Why this was worth an ADR

A future reader will find a UI guard asking the backend a question it could
answer in one line, and a display cache calling a predicate from a module it
otherwise does not depend on. Both look like accidental complexity and are not.

## Amendment 2026-09-30 — every custody numeric boundary, not only flatness (#2749)

Decision 1 records one numeric rule. The Clerk's other numeric boundaries
kept their reasons only in reference notes and code comments (#2745 entry 2).
Each one below is backend-owned under Decision 2 and decided here. Decision 1
is unchanged.

1. **Fill-quantity equality uses an absolute `1e-9`, the same as flatness.**
   The constant is `FILL_QTY_EPSILON = 1e-9` with `rtol=0`
   (`app/broker/alpaca/clerk/sqlite/execution_coverage.py`). It is shared by
   `manual_order_completion.py`, `order_evidence.py` and the fill fold.
   - Alpaca reports `filled_quantity` as a cumulative total. The fold
     recovers the delta as `cumulative − Σ prior recorded fills`.
   - A repeated observation of the same cumulative state can therefore
     differ by float64 residue, about `1e-12` to `1e-13` at these
     magnitudes, rather than by exactly zero. `1e-9` absorbs that residue
     without treating a real fractional-share remainder as complete.
   - The tolerance is absolute, not relative, because share quantities are
     absolute. A relative tolerance would hide drift on small positions.
   - A delta below the epsilon is never inserted. `fill_id` formats the
     cumulative quantity to the same nine decimal places, so identical
     states deduplicate.
   - `FILL_QTY_EPSILON` is also the "pinned execution tolerance" in ADR
     0035's `EXACT_REPLACES_CUMULATIVE` rule. The many-to-many coverage proof
     uses `QTY_ATOL` and `PRICE_ATOL`, both `1e-9` with `rtol=0`.
2. **A cumulative broker fill is priced on its delta.** The formula is
   `delta_price = (cum_qty × cum_avg − prior_qty × prior_avg) / delta_qty`,
   with the prior cost basis summed from the order's recorded fills
   (`sqlite/folds.py::_fold_order_fill_observed`). Alpaca's
   `filled_avg_price` is an average over the whole order. Copying it as the
   price of the latest clip is wrong whenever an order fills in clips at
   different prices.
3. **At the same quantity, an average-price gap below one valid price
   increment of the reported average is vendor rounding; a gap of one
   increment or more is a conflict.**
   - The tolerance is `total_price_conflict_atol(reported_avg_price)` per
     share, `rtol=0` (`sqlite/order_evidence.py`, #2460, #2770): $0.01 when
     the broker's reported average is at or above $1, $0.0001 below.
   - The increment is Alpaca's price precision, read from the canonical
     tick rule, `price_increment` in `app/broker/alpaca/marketable_limit.py`.
   - Alpaca publishes prices in cents at or above $1, so a smaller gap
     cannot be told apart from rounding there. Below $1 the venue tick is
     $0.0001, so a one-cent rule there read a real sub-cent discrepancy,
     such as $0.005 on a $0.50 fill, as rounding (#2770).
   - A gap at or above the increment records a durable
     `EXECUTION_PRICE_CONFLICT` episode. At or above $1 this is the same
     basis as the reconciliation `FILL_PRICE_DRIFT` default (ADR 0069 §3).
   - The episode never rewrites recorded fills and never forbids reductions,
     because the quantity is the one thing both sides agree on. It clears
     when a later total agrees.
4. **Budget money is exact `Decimal` arithmetic.**
   `app/broker/alpaca/clerk/money.py` is the one normalization boundary (PRD
   #2540, #2545).
   - Legacy floats enter as `Decimal(str(x))` before any arithmetic.
   - A fixed 1,400-digit local context traps inexact arithmetic instead of
     silently changing an admission answer.
   - Consent is a positive whole-cent amount and never rounds.
   - Required cents round up and spendable cents round down, and only at the
     boundary where they are acted on.
   - Affordability compares exact amounts with **no epsilon**.

   Why: an epsilon on money admits any shortfall smaller than itself, and a
   rounded consent is not the amount the operator agreed to. Precision
   already lost in historical SQLite REAL values cannot be recovered, and
   none is claimed. ADR 0059's budget amendment ("whole-cent dollar
   commitment") rests on this rule.

## Amendment 2026-10-01 — a manual chain's total, on its head's quantity (#2786)

1. **A filled manual chain's exact executions set its position.** When the
   chain head reports `filled` and the chain's distinct exact executions
   satisfy `|fsum(q_exact) − Q_head| < QTY_ATOL` (`1e-9` shares, `rtol=0`),
   `EXECUTION_COVERAGE_CHAIN_TOTAL_PROVEN` replaces every cumulative-recovery
   row of the leg with them and moves its position by
   `fsum(q_exact) − fsum(q_effective)`
   (`execution_coverage.py::chain_total_proves_coverage`). It is the one
   coverage proof that may move the position: a replacement's `filled_qty`
   can leave out its original's fills, so a cumulative folded from it
   under-credits the chain, while the head's `qty` is the chain's whole
   total. Only the head's observation carries `Q_head`; the transition
   records it and the fold re-reads everything else. An unreplaced leg is a
   chain of one.
2. **A replaced manual leg completes only against its head's quantity.** No
   row a fold reads holds it, so the coverage folds no longer complete a
   replaced leg against the accepted quantity, which could end a raised leg
   early; the head's next acknowledgement completes it.
