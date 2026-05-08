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
