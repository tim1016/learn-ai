# [Codex] Running executable identity versus on-disk qualification

Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. Follow-up to the independent seam investigation. No runtime stack, account state, prior audit, vendor network, or production checkout was accessed.

## Finding B1 — A source receipt does not identify already imported code

- **Claim:** Signal Program admission can return `PROVEN` for the qualified source currently on disk while the process executes a different, previously imported decision function.
- **Goal:** Correctness; architecture; backtest/live equivalence.
- **Severity:** High — an entire class of loaded-code/source-code divergence is invisible to a gate that claims to identify the running build. The reproduction demonstrates a different decision predicate, not a submitted broker order.
- **Evidence — reproduced:** [reproduce_codex_2418_loaded_code.py](https://github.com/tim1016/learn-ai/blob/research/codex-2418-runtime-proof/PythonDataService/scripts/reproduce_codex_2418_loaded_code.py) starts a fresh interpreter in the isolated clone, temporarily changes the EMA gap predicate from a $0.20 floor to twice that floor, imports the actual algorithm normally, then restores the exact qualified file bytes in a `finally` block. It changes neither the admission function nor the qualification manifest. The production seal/build fixture and a fresh `prove_running_program_build` call both accept the restored bytes. The loaded algorithm rejects a $0.30 gap while the qualified source accepts it: `proof=PROVEN, actual_gap_pass=False, qualified_gap_pass=True`. The expected safety invariant fails. Git confirms that no production source diff remains.
- **Evidence — proven:** [signal_program_admission.py:490–502](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/signal_program_admission.py#L490) hashes `candidate.read_bytes()` at admission. [Receipt selection and result](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/signal_program_admission.py#L400) match those hashes and return `PROVEN`; they do not bind an import-time identity. [compose.fleet.dev.yaml:20–30](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/compose.fleet.dev.yaml#L20) deliberately disables reload while bind-mounting the host app source. Its comment calls the bind a convenience for the next restart; that is precisely the period in which on-disk bytes and loaded modules can differ.
- **Why it matters:** A checkout update followed by Start/Resume can attest to source the long-lived process has not imported. Matching a golden trace root, sealed parameters and current file hashes cannot repair this missing link. This is not evidence that every deployment update bypasses admission: a simultaneous program-version/trace-root change can correctly refuse. It shows the source-only proof is insufficient in the supported mutable-source topology.
- **Decision rationale, quoted before challenge:** [ADR 0043 Decision 2](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0043-signal-program-build-proof-and-legacy-seal-migration.md#L89) chooses a “loaded-file digest set” because the alternatives require “container/image introspection, or a trustworthy `git` state at runtime” and cover files unrelated to decision math. The narrow semantic closure and lack of runtime host powers remain sound reasons. The unsupported step is equating a later read of mutable source with the code already loaded. No image introspection or full-repository hash is required to close that gap.
- **Recommendation:** Preserve the narrow decision closure but bind qualification to an immutable process code generation: load and attest from a fixed snapshot, capture and retain the imported generation identity, and refuse admission if the generation cannot be proved. Treat source replacement as requiring a controlled process transition. Verify all lazily imported closure members belong to that same generation. Keep deployment provenance separate from proof of the actually executing generation.
- **Confidence:** High for the production admission false proof and explicit bind-mount configuration. No production deployment sequence or broker run was inspected. A verified immutable per-process source snapshot or process-generation fence before every admission would refute reachability; the current traced code has neither. Monkeypatch attacks are not required by the reproduction; it uses ordinary import and file replacement.

## Checks and limits

- The guarded fresh-interpreter reproduction produces one expected assertion failure. An initial run lacked the dummy required Polygon configuration value and failed before admission; the final sanitized command uses `POLYGON_API_KEY=test-only`, without reading any environment file or making a network call.
- The isolated source file is restored even if import raises. No data files or broker services are opened. No infrastructure was started.
- The preceding seam ticket's 65 passing admission/replay/tail tests remain useful counter-evidence: ordinary file drift and receipt mismatch are tested and rejected. They do not test a process retaining older executable objects after the source changes.
- This recommends satisfying ADR 0043's running-code claim; it does not reverse the IBKR-data/Alpaca-execution boundary or require broader build hashes.

Reproduce from the isolated clone's `PythonDataService` using the review guard:

```sh
env -i PATH="$PATH" HOME=/Users/inkant/codex-review-20260924/review \
  PYTHONDONTWRITEBYTECODE=1 POLYGON_API_KEY=test-only DATA_PLANE_CONTROL_SECRET='' \
  TMPDIR=/Users/inkant/codex-review-20260924/review/test-temp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  /Users/inkant/codex-review-20260924/control/guarded_run.py \
  scripts/reproduce_codex_2418_loaded_code.py
```
