# research-artifact-pr4 — pre-migration runs/ golden bytes

## What this fixture is

Pre-PR4 byte-for-byte capture of `ledger.json` and `result.json` as
produced by `app.research.runs.storage.save_run` against a fully-
populated, deterministic `RunLedger` + `BacktestRunResult`. PR 4
migrates `runs/` onto the shared `app/research/artifact/` seam; the
acceptance bar from
`docs/architecture/research-artifact-seam.md` § "Per-PR acceptance
bar" requires the migrated `save_run` to write byte-identical files
for the same inputs — preserving the canonical-JSON hash on which
existing replay addresses depend.

The byte-equivalence test lives at
`PythonDataService/tests/research/artifact/test_runs_byte_equivalence.py`.

## Source

- **Reference:** `app.research.runs.storage.save_run` at master
  commit `6304ee3` (the SHA immediately before PR 4 lands).
- **Capture date:** 2026-05-26.
- **Capture script:** see commit message of
  `chore(research-artifact): capture pre-PR runs/ golden artifacts`
  — the script lived only in `/tmp` and was thrown away after the
  bytes were committed, by design (a re-run of the script must
  reproduce the same bytes, because both the inputs and the
  serializer are deterministic; we don't need the script in-tree).

## Inputs

The deterministic fixture is constructed in the test itself
(`_deterministic_ledger()` / `_deterministic_result()`). The fields
chosen exercise:

- `run_id` and `parent_run_id` as distinct 32-hex strings so the
  descriptor's `id_pattern` validation path is exercised (the same
  regression-test path PR 1's commit `1146a95` added).
- Every optional ledger field populated (lineage, prediction-set,
  result hashes, completed timestamps) so the canonical serialisation
  is exhaustive.
- A non-trivial `strategy_spec_json` payload (nested dict + list)
  so any future change to JSON encoding visibly breaks the
  comparison.
- `equity_curve`, `drawdown_curve`, `trades`, `metrics`, and
  `log_lines` populated with deterministic values so the result file
  exercises every field shape.

## Assumptions

- No timezone conversion: every `*_ms` field is `int64 ms UTC` per
  `.claude/rules/numerical-rigor.md`. Wall-clock time never enters
  the fixture.
- `model_dump(mode='json')` followed by `json.dumps(..., ensure_ascii=False)`
  is the canonical-bytes formula. This matches what the pre-seam
  `_atomic_write_json` did and what `ArtifactStore.save` continues to do.

## Lifecycle

- **Regenerate only with justification.** If the canonical
  serialisation of `RunLedger` or `BacktestRunResult` legitimately
  changes (Pydantic schema migration, new fields), the commit that
  regenerates these bytes must explain why in its message and bump
  the relevant `schema_version` field on the model. A test failure
  alone is NOT a justification to regenerate — see
  `.claude/rules/numerical-rigor.md` § "Anti-patterns to reject".
- **Never hand-edit.** If the bytes are wrong, the reference
  (pre-PR4 `save_run`) is rerun against the same deterministic
  fixture.
