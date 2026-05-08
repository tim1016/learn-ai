# PR Review Escalations

Comments too complex for the monitor agent to resolve autonomously. Claude handles these.

---

## PR #161 — missing docs/references links (comment 3207179654)

**Comment (3207179654):** Add explicit `docs/references/` links for the newly certified ports.

> The updated canonical rows cite tests/fixtures, but they still don't point to a `docs/references/` note for these ported calculations (IND-001..003, BS-004..007, ENG-002..005). Please add and link those notes in the registry rows.

**Why complex:** Requires creating new docs/references/ markdown files for 11 fixtures across 3 categories (IND-001–003, BS-004–007, ENG-002–005), each citing the reference source, tolerance, and golden-fixture provenance. Then each row in math-sources-of-truth.md needs a backlink.

**Date:** 2026-05-08

**Status:** ✅ RESOLVED — commit 38edac2 created all 11 reference docs and updated math-sources-of-truth.md with backlinks. PR comment posted 2026-05-08.

---

## PR #161 — rtol discarded in _assert_sequence (comment 3207179707)

**Comment (3207179707):** `rtol` is silently discarded in `_assert_sequence`.

> `_load` returns `rtol` but `_assert_sequence` only uses `atol`. All current indicator fixtures pin `rtol=0.0`, so no test failure is possible today. But the moment any indicator fixture is regenerated with `rtol > 0`, these tests will silently under-check relative error.

**Why complex:** Touches tolerance-checking formula in the golden fixture validation harness — any mistake silently changes which tests pass/fail.

**Date:** 2026-05-08

**Status:** ✅ RESOLVED — commit 38edac2 added `rtol` parameter to `_assert_sequence`, applies `atol + rtol * abs(o)`, updated all 3 call sites (IND-001/002/003). PR comment posted 2026-05-08.

---

## PR #162 — IV-001/IV-002 tolerance 1e-6 vs 1e-9 (comment 3208850480)

**Comment (3208850480):** IV-001 and IV-002 manifest uses atol=1e-6 but PR described 1e-9.

**Why complex:** Tolerance for numerical solvers is inherently solver-convergence-limited; needed to confirm 1e-6 is correct vs. tightening to 1e-9 would cause spurious failures.

**Date:** 2026-05-08

**Status:** ✅ RESOLVED — 1e-6 confirmed intentional (Newton-Raphson converges to ~1e-7; 1e-9 would cause spurious failures). No code change. Replied to comment 2026-05-08 explaining solver-limited rationale.

---

## PR #162 — RV-004 attribution conflates error types (comment 3208850485)

**Comment (3208850485):** Attribution rationale conflated oracle-vs-canonical float error (~1e-15) with model-vs-formula discretization error (~1e-4).

**Why complex:** Separating two distinct numerical error sources requires understanding the CBOE formula's discrete-sum vs. continuous-integral distinction and what atol=1e-6 actually guards against.

**Date:** 2026-05-08

**Status:** ✅ RESOLVED — commit 6d6453f rewrote RV-004/v1/attribution.md tolerance section to clearly separate (1) oracle-vs-canonical ~1e-15 and (2) model-vs-formula ~1e-4; explains atol=1e-6 is a safety margin above (1), not a license for (2). Replied to comment 2026-05-08.

---

## PR #162 — RV-001 test_nan_before_window misses last warmup bar (comment 3208850490)

**Comment (3208850490):** `range(self._WINDOW - 1)` misses bar index `_WINDOW - 1` which should still be NaN.

**Why complex:** Required understanding that close_to_close computes log-returns first (NaN at index 0), so rolling window needs `window` returns and first valid value is at index `window` not `window-1`.

**Date:** 2026-05-08

**Status:** ✅ RESOLVED — commit 6d6453f changed to `range(self._WINDOW)` to cover all 10 NaN bars (indices 0..9) for RV-001 with window=10. Replied to comment 2026-05-08.
