# PR Review Escalations

Comments too complex for the monitor agent to resolve autonomously. Claude handles these.

---

## PR #161 — feat(fixtures): Phase 1 golden fixture system — BS-004–007, ENG-002–005, IND-001–003

**Comment (3207179654):** Add explicit `docs/references/` links for the newly certified ports.

> The updated canonical rows cite tests/fixtures, but they still don't point to a `docs/references/` note for these ported calculations (IND-001..003, BS-004..007, ENG-002..005). Please add and link those notes in the registry rows.
>
> As per coding guidelines, "Every ported indicator, strategy, or calculation must ship with (a) a golden fixture derived from the reference, (b) a tolerance-pinned test, and (c) a citation in `docs/references/`" and "Every port from a reference source must ship with (a) a golden fixture test, (b) a `docs/references/` note, and (c) the tolerance used and why".
>
> Also applies to: 34-34, 48-53

**Why complex:** Requires creating new docs/references/ markdown files for 11 fixtures across 3 categories (IND-001–003, BS-004–007, ENG-002–005), each citing the reference source, tolerance, and golden-fixture provenance. Then each row in math-sources-of-truth.md needs a backlink. This is an architectural documentation obligation — understanding which references are already covered by existing docs/references/ files (e.g., indicators may already have a shared note) vs. which need new files requires code architecture context.

**Date:** 2026-05-08

---

## PR #161 — feat(fixtures): Phase 1 golden fixture system — BS-004–007, ENG-002–005, IND-001–003

**Comment (3207179707):** `rtol` is silently discarded in `_assert_sequence`.

> `_load` returns `rtol` but `_assert_sequence` only uses `atol`. All current indicator fixtures pin `rtol=0.0`, so no test failure is possible today. But the moment any indicator fixture is regenerated with `rtol > 0`, these tests will silently under-check relative error.
>
> Proposed fix: update `_assert_sequence` signature to accept `rtol`, apply `abs(c - o) <= atol + rtol * abs(o)`, and update all call sites.

**Why complex:** Touches test logic in `test_indicator_fixtures.py` — adding `rtol` to `_assert_sequence` and updating all call sites (`_assert_sequence(canonical, oracle, atol, label)` → `_assert_sequence(canonical, oracle, atol, rtol, label)`). While mechanically simple, it modifies the tolerance-checking formula in the golden fixture validation harness. Any mistake here silently changes which tests pass/fail. Requires understanding whether other `_assert_sequence` call sites across the fixture test suite need the same treatment (the same pattern may exist in other fixture test files), and whether the change belongs in all fixture test modules consistently. Classifying as complex to avoid introducing inconsistency.

**Date:** 2026-05-08
