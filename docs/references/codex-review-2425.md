# [Codex] CSV comparison coverage and validation-grade integrity

Baseline `10b5f31b529c8507bd19bb24015f3d85fa9aba43`; independent follow-up to Stocks. This is the active downloadable Data Lab comparison, not the deployment validation gate. No prior audits or runtime state were read.

## Finding C1 — Agreement is graded without establishing comparable evidence

- **Claim:** The CSV comparison can grade “Excellent” after discarding nearly all indicator observations, or without a shared timestamp key, and duplicate keys inflate the reported match rate above 100%.
- **Goal:** Correctness; trading intelligence; validation evidence quality.
- **Severity:** High for a realistically wrong validation result and unverified comparison basis. The report is advisory and does not authorize broker execution; no deployment bypass is claimed.
- **Evidence — reproduced:** [test_codex_2425_csv.py](https://github.com/tim1016/learn-ai/blob/research/codex-2425-csv/PythonDataService/tests/review/test_codex_2425_csv.py) runs the real report service on synthetic uploads. Of 1,000 timestamp-aligned rows, one file has 999 missing indicator values and one matching value; the report shows 100% row alignment, one compared value, and an Excellent overall grade. Two identical timestamp rows on each side generate four comparison rows, 200% match rate and −2 unmatched rows. Two files with different clock column names and disjoint instants receive positional alignment and an Excellent grade solely because their two numeric values match. Three intended evidence invariants fail; correctly aligned agreement and disagreement controls both pass.
- **Evidence — proven:** [validation_service.py:37–54](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/validation_service.py#L37) uses an unconstrained inner merge or a positional fallback. [Lines 71–87](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/validation_service.py#L71) retain only pairs where both values are numeric; missing-value counts are gathered but are not rendered in the per-field table. [Lines 218–254](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/validation_service.py#L218) calculate coverage from merged row count and grade only valid pairs. [dataset.py:607–627](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/routers/dataset.py#L607) passes uploads directly to the service. The active [validate.component.ts:196–220](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/Frontend/src/app/components/data-lab/validate/validate.component.ts#L196) calls that route and displays/downloads its report.
- **Why it matters:** An implementation that fails to produce values, uses the wrong dates, or duplicates source rows can receive the same headline as a complete, unique, timestamp-equivalent comparison. Per-field sample size and alignment method are visible, which makes some limitations discoverable by a careful reader; this is why the report is not characterized as an invisible automated execution gate. The report still makes an unqualified overall accuracy claim while omitting missing-value disagreement from its grade.
- **Recommendation:** Make comparison validity a prerequisite and an explicit result: unique typed timestamps, declared units, one-to-one alignment, field mapping, expected coverage and warmup masks. Report missingness disagreements and unmatched rows separately from value error. Retain positional comparisons only as explicitly unverified diagnostics, and name matched-pair agreement separately from overall validation. Suppress a validation grade when comparable evidence is insufficient rather than grading the surviving subset.
- **Confidence:** High for all three production-service outputs. A downstream consumer that restricts this artifact to a visibly conditional matched-pair diagnostic could reduce the impact, but the inspected UI and download contain the same “Overall Accuracy”/“Overall grade” report. No uninspected downstream release or trading gate is assumed.

## Controls, boundaries, and validation

- Full unique aligned matches grade Excellent; deliberately divergent aligned values grade Significant divergence. The numeric comparison itself is not the reproduced problem.
- No duplicated implementation or a new oracle was introduced. Synthetic tests compare report behavior with explicit evidence invariants.
- Result: **3 expected failures, 2 passing controls**, under the shared network/file guard, sanitized environment, and `pytest --noconftest` against this one self-contained test file. The disabled unused asyncio plugin produces one configuration warning. No application lifespan or external request runs.
- The report is an advisory CSV comparison; the independent Strategy Validation admission path was not found to consume it automatically. Different user files may use seconds or unsupported column names; that increases the need for a typed alignment boundary, rather than being evidence that row position represents time.
- This recommendation supports existing temporal uniqueness and warmup-equivalence rules. It does not reverse a documented design decision or prescribe a new numerical tolerance.

Reproduction command, from isolated `PythonDataService`:

```sh
env -i PATH="$PATH" HOME=/Users/inkant/codex-review-20260924/review \
  PYTHONDONTWRITEBYTECODE=1 POLYGON_API_KEY=test-only DATA_PLANE_CONTROL_SECRET='' \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TMPDIR=/Users/inkant/codex-review-20260924/review/test-temp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  /Users/inkant/codex-review-20260924/control/guarded_run.py -m pytest --noconftest \
  -o addopts='' --basetemp=/Users/inkant/codex-review-20260924/review/test-temp/csv \
  tests/review/test_codex_2425_csv.py -q
```
