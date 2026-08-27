# ADR 0036: One flatness boundary, owned by the backend

**Status:** Accepted

- **Date:** 2026-08-17
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1597](https://github.com/tim1016/learn-ai/issues/1597); the
  numeric authority census in `docs/audits/numeric-authority-census-2026-08-17.md`.
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
   registered in `docs/math-sources-of-truth.md`; the conformance obligation
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

6. **`math-sources-of-truth.md` row 95 needs widening.** It records that "Angular
   renders the Python-authored plan and performs no closing-quantity
   calculation" — true, but narrower than Decision 2, which bars Angular from
   *any* flatness classification, not only closing quantities.

## Why this was worth an ADR

A future reader will find a UI guard asking the backend a question it could
answer in one line, and a display cache calling a predicate from a module it
otherwise does not depend on. Both look like accidental complexity and are not.
