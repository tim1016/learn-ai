# ML Predictions Functionality Validation — 2026-05-12

**Mode:** ad-hoc (outside skill state machine — user authorized while sleeping)
**Started:** 2026-05-12 (overnight)
**Ended:** 2026-05-12 (same session)
**Stop reason:** complete
**Analyst:** auto-research-tick (claude-opus-4-7)
**Scope:** ML predictions feature (PR #207–#221 + Phase 3.5 Path A on branch `feat/phase35-impl-path-a`)

---

## 1. Executive Verdict

**ML predictions feature is functionally correct and numerically validated where the design says it should be.** 148/148 targeted tests pass; the canonical hash determinism contract holds; the QC parity reconciler classifies divergences correctly across all 8 categories; round-trip P&L tolerance propagation is algebraically sound.

**Open gaps** (in order of remediation priority):

| ID | Severity | Area | Summary |
|---|---|---|---|
| ML-V-001 | **P1** | inventory | New canonical math (PredictionSet artifact, IbkrEquityCommissionModel, QcReconciler round-trip P&L pairing + tolerance propagation, FixtureDataReader) is **not listed** in `docs/math-sources-of-truth.md`. Phase A/C/D/E1 have rows; Phase 3.0/3.5 doesn't. |
| ML-V-002 | **P2** | provenance | `app/research/parity/qc_reconciler.py` and `app/research/parity/fixture_data_reader.py` lack the 4-field provenance block. `ibkr_commission.py` is clean. The two missing files have design-spec pointers in module docstrings but not the canonical Formula/Reference/Canonical/Validated-against labels. |
| ML-V-003 | **P2** | wire-fidelity | `RunLedger.prediction_set_hash` exists in Python (`ledger.py:201`, OpenAPI confirms), persists to JSON, and returns over HTTP — but Frontend `strategy-runs` types/services do not declare `predictionSetHash`. The field is silently stripped at the TS deserialization boundary. Forward-looking — no UI consumer today (authority doc § 9), but the trail breaks the moment Phase 4 ships. |
| ML-V-004 | **P3** | timestamp | `app/research/parity/fixture_data_reader.py:95` uses `pd.to_datetime(..., utc=False, errors="raise")` — triggers the ban-list grep. The intent is "parse QC's NY-local CSV strings as naive, then attach `tzinfo=_NY` manually downstream" (lines 116–120). Documented in the module docstring; doesn't cross a wire/storage boundary. Acceptable but worth an inline `# justified-deliberate-naive-parse-NY-local` comment so the ban-list grep doesn't keep tripping. |

---

## 2. Environment

| Item | Value |
|---|---|
| Git branch | `feat/phase35-impl-path-a` |
| Git commit | `3d1193b7a35faac6bfa843e430f7a3b7e8170d17` |
| Containers | All 5 healthy (postgres, redis, polygon-data-service, my-backend, my-frontend) — uptime ~17h |
| Python service | http://localhost:8000 — OpenAPI confirms `RunLedger.prediction_set_hash` field |
| Playwright | Not used — no ML-specific UI surface in v0.5 per authority doc § 9; Build Alpha UI was validated 2026-05-07 |

---

## 3. Scope and Discovery

### What was audited

ML feature module surface per `docs/ml-predictions-authority.md` § 3:

- **`PythonDataService/app/research/ml/`** — `loader.py`, `artifact.py`, `coverage.py`, `generators/quantconnect_fixture.py`, `generators/deterministic_rule.py`, `generate_prediction_set.py`
- **`PythonDataService/app/engine/strategy/spec/`** — `schema.py` (`PredictionRef.lookup`), `primitives.py` (per-ref lookup dispatch), `evaluator.py` (`SpecAlgorithm` injection), and engine fill-mode wiring
- **`PythonDataService/app/research/parity/`** — `fixture_data_reader.py`, `ibkr_commission.py`, `qc_reconciler.py`
- **`PythonDataService/app/research/runs/`** — `ledger.py` (`prediction_set_hash` field at v1.1), `runner.py` (orchestration)
- **Test suites** — `tests/research/ml/` (14 files) and `tests/research/parity/` (5 files)
- **Fixtures** — `artifacts/predictions/qc_aapl_gbm_v001/`, `artifacts/predictions/qc_spy_precomputed_v001/`, `tests/fixtures/golden/qc-precomputed-predictions/`, `tests/fixtures/golden/qc-aapl-phase3/`

### Module-to-authority-doc check

Module surface matches `docs/ml-predictions-authority.md` § 3 exactly. No drift between authority doc and code reality.

---

## 4. Static Findings

### 4.1 Tests — 148/148 pass

```
podman exec polygon-data-service python -m pytest /app/tests/research/parity /app/tests/research/ml -v
======================= 148 passed, 6 warnings in 1.88s ========================
```

Includes every test referenced in authority doc § 8: `test_quantconnect_fixture_determinism`, `test_fixture_data_reader`, `test_ibkr_commission`, `test_qc_reconciler`, `test_qc_fixture_smoke`, `test_qc_aapl_phase3_trade_parity`, `test_loader`, `test_coverage`, `test_e2e_replay`, `test_artifact_generator_meta`, etc.

No xfail. No skipped tests (other than warnings from upstream deprecations — pandas-ta `pkg_resources`, Pydantic class-based config in third-party code, FastAPI `regex=` parameter — none in the ML feature itself).

### 4.2 Tolerance pinning — clean

- **`tests/research/parity/`** uses `Decimal` exact arithmetic throughout (no `np.allclose`, no `pytest.approx` on float comparisons). Round-trip P&L pairing tolerance is computed in `Decimal` from per-share atols. **Strict-float equivalent or stronger.**
- **`tests/research/ml/test_quantconnect_fixture_parity.py:92`** uses `math.isclose(imported_value, qc_value, abs_tol=1e-9, rel_tol=0)` with inline justification ("spec D8") and forbids loosening in the module docstring (lines 7, 57). **Matches numerical-rigor.md default.**

### 4.3 Provenance blocks — ML-V-002 (P2)

| File | Has 4-field block? | Notes |
|---|---|---|
| `app/research/parity/ibkr_commission.py` | **yes** | Formula at line 17, Reference at line 3, Canonical at line 23, Validated against at line 24 — exemplary |
| `app/research/parity/qc_reconciler.py` | **no** | Module docstring points at design spec (`docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md`) but lacks the labeled block. Contains real math: round-trip realized P&L (`(exit - entry) * shares - fees`) at line 619, propagated atol formula at lines 620–622, plus the 8-category `DivergenceCategory` enum which is a math contract |
| `app/research/parity/fixture_data_reader.py` | **no** | Module docstring documents bar semantics + tz conventions but lacks the labeled block |
| `app/research/ml/loader.py` | **no** | Pure plumbing (hash-check + index build); arguable whether it's "canonical math" but it owns the leakage and hash-tampering invariants |
| `app/research/ml/artifact.py` | partial | Has the int64-ms-UTC note (line 7) but not the 4-field block; canonical hash math (`compute_rows_hash`, `compute_prediction_set_hash`) lives here |
| `app/research/ml/coverage.py` | **no** | Pure structural check; arguable not a math file |

Fix proposal (not auto-applied): add 4-field blocks to `qc_reconciler.py` and `artifact.py` (the two with real math). The other files can keep their existing module docstrings.

### 4.4 Registry inventory — ML-V-001 (P1)

`docs/math-sources-of-truth.md` has rows for Phase A (run-ledger hashing, row 55), Phase C (walk-forward, row 56), Phase D (Monte Carlo, row 57), Phase E1 (baselines, row 58). **No row for the ML predictions feature** (PredictionSet artifact + hash determinism, IbkrEquityCommissionModel, FixtureDataReader, QcReconciler round-trip P&L pairing/tolerance propagation, FillMode.NEXT_SESSION_OPEN).

Per `.claude/rules/numerical-rigor.md` Phase 1 ("Canonical math inventory & source-of-truth gaps"), unregistered canonical math doing live computation routes to **P1**.

Fix proposal: add 4–5 rows to the registry under a new "ML predictions & QC parity" section, citing:
- `app/research/ml/artifact.py::compute_prediction_set_hash` + `compute_rows_hash` → SHA-256 / `app.research.runs.hashing.hash_payload` / `tests/research/ml/test_artifact.py` + `test_quantconnect_fixture_determinism.py`
- `app/research/parity/ibkr_commission.py::IbkrEquityCommissionModel` → QC IBKR docs / canonical here / `test_ibkr_commission.py`
- `app/research/parity/qc_reconciler.py` round-trip P&L + tolerance propagation → design spec / canonical here / `test_qc_reconciler.py` + `test_qc_aapl_phase3_trade_parity.py`
- `app/research/parity/fixture_data_reader.py` → LEAN bar-semantics convention / canonical here / `test_fixture_data_reader.py`
- `FillMode.NEXT_SESSION_OPEN` + `PredictionRef.lookup="next_after_bar_close"` → design spec / canonical in `app/engine/strategy/spec/engine.py` and `primitives.py` / acceptance test in `test_qc_aapl_phase3_trade_parity.py`

### 4.5 Timestamp ban-list scan — ML-V-004 (P3)

Across `app/research/ml/`, `app/research/parity/`, `app/research/runs/`, `app/engine/strategy/spec/`:

| Pattern | Hits | Verdict |
|---|---|---|
| `datetime.utcnow` | 0 | clean |
| `datetime.utcfromtimestamp` | 0 | clean |
| `datetime.now()` (no `tz=`) | 0 | clean |
| `pd.to_datetime(...)` without `utc=True` | 1 (`fixture_data_reader.py:95`) | deliberate, see below |
| `.strftime(".*Z")` on naive | 0 | clean |

`fixture_data_reader.py:95` is `pd.to_datetime(frame["time"], utc=False, errors="raise")`. Input is QC's NY-local CSV time string. The naive-parse is intentional — line 116 then attaches `tzinfo=_NY` manually. Documented in module docstring lines 12–19. Doesn't cross a wire/storage boundary (output is a `TradeBar` consumed in-process by the engine). **Acceptable, but the grep hit will recur in future audits unless an inline justification comment is added.**

At QC ingest boundary in `qc_reconciler.py:320–343`, both ISO-string and numeric `time` payloads are normalized to `int64 ms UTC` with explicit second/ms disambiguation and ambiguous-range rejection (line 334–338). **Clean — exemplary ingestion-boundary handling.**

### 4.6 Wire fidelity — ML-V-003 (P2)

| Layer | `prediction_set_hash` present? | Evidence |
|---|---|---|
| Python `RunLedger` Pydantic model | **yes** | `ledger.py:201` (schema v1.1) |
| Python persistence (`ledger.json` on disk) | **yes** | Pydantic dump preserves it |
| Python HTTP response | **yes** | OpenAPI schema confirms field on `RunLedger` |
| .NET Backend | **n/a** | `/api/research/strategy-runs` is Python→Frontend direct; no .NET wrapper (grep returned 0 files in `Backend/`) |
| Frontend TS types/services | **no** | grep `predictionSetHash` / `prediction_set_hash` in `Frontend/src` → 0 hits. Field is silently dropped by snake→camel-aware response mapper because the target interface doesn't declare it |

The bug is latent: today no UI consumer needs the hash (authority doc § 9 confirms v0.5 has no ML UI). Phase 4 (multi-symbol top-N ranking) is when this becomes user-facing. Fix proposal: add `predictionSetHash: string | null` to the `RunLedger` TS interface in `Frontend/src/app/services/strategy-runs.types.ts` (or wherever the response shape is declared) in the same PR that introduces the first UI consumer.

---

## 5. Numerical Trace

### 5.1 `prediction_set_hash` determinism

The headline numerical claim of v0.5 is "re-importing the same `qc_export.json` produces the same hash." Pinned at:

```
b8252cfa9a749f5bf592602f3aebc2b3a4ccc6bb0cd41da48a6db7a581342e0e
```

(see `artifacts/predictions/qc_aapl_gbm_v001/manifest.json:30` and the test fixture `tests/research/ml/fixtures/qc_known_hashes.json`).

Test: `test_quantconnect_fixture_determinism::test_repeated_import_produces_identical_hash_and_manifest` — **PASSED** in this run. The hash is canonical-JSON SHA-256 over the manifest minus `prediction_set_hash` itself (chicken-and-egg avoidance per `artifact.py:161–170`).

### 5.2 Round-trip P&L tolerance propagation

`qc_reconciler.py:619–622` computes:

```python
realized_pnl = (exit_price - entry_price) * shares - entry_fee - exit_fee
propagated_atol = (|entry_qty| + |exit_qty|) * per_share_pnl_atol + 2 * commission_atol
```

This is the triangle inequality applied to fill-level atols, which means per-fill parity algebraically implies P&L parity. The docstring at line 654 notes this explicitly. Defaults: `per_share_pnl_atol=0.01`, `commission_atol=0.01`. For a round-trip of (100 buy + 100 sell), the propagated tolerance is `200 × $0.01 + 2 × $0.01 = $2.02`. **Justified, not loosened.**

Test: `test_qc_aapl_phase3_trade_parity::test_phase35_aligned_buy_pinned_2026_02_10` validates that a 2026-02-10 buy at $273.18 (ours) vs $273.24 (QC) — a $0.06 drift — passes within the bid-ask tolerance. **PASSED.**

### 5.3 Coverage check correctness

The `assert_bar_clock_coverage` function (`coverage.py:41`) validates two lookup modes (`exact_bar_close`, `next_after_bar_close`) and supports mixed-mode specs. Tests assert all four error paths fire with the right message content (`test_coverage_next_after_no_later_row_raises_with_fired_ts_in_message`, etc.). **All 11 coverage tests pass.**

### 5.4 Hash chicken-and-egg invariant

`compute_prediction_set_hash` (`artifact.py:161`) drops the `prediction_set_hash` field from the manifest dict before hashing. Tested by `test_prediction_set_hash_excludes_self_field`. **PASSED.**

---

## 6. Recommendations (ordered by impact, not auto-applied)

1. **Land ML-V-001** — add 4–5 rows to `docs/math-sources-of-truth.md` for the ML predictions and QC parity canonical math. Highest impact because every future ML-feature reviewer will look at the registry first and find nothing.

2. **Land ML-V-002** — add 4-field provenance blocks to `qc_reconciler.py` (round-trip P&L math + DivergenceCategory contract) and `artifact.py` (hash math). Skip `loader.py`, `coverage.py`, `fixture_data_reader.py` — they don't compute canonical numerical answers.

3. **Land ML-V-003 in the same PR as the first ML UI consumer** — adding `predictionSetHash` to TS types without a UI consumer is dead code; adding the UI consumer without the type is a wire-fidelity violation. They must land together. The Frontend `RunLedger` TS interface is the right place.

4. **ML-V-004** — one-line inline justification comment at `fixture_data_reader.py:95` to suppress future ban-list grep hits. Low priority.

5. **Capture-runbook posterity** — the QC capture runbook (`docs/references/qc-aapl-phase3-capture-runbook.md`) is currently a manual workflow because free-tier QC has no API token (authority doc § 10). Phase 3.5+ multi-day fixture is not being pursued (decision 2026-05-12; QC free tier's minute-data trailing window plus the cost/scope tradeoffs make this not worth the effort — see authority doc § 10 and the qc-aapl-phase3 reconciliation doc for the full rationale). If that decision is ever reversed, update the runbook in the same PR.

6. **Not a deficiency, but worth noting** — the `_pair_round_trips` Phase 3 invariant ("single-position-at-a-time long-only — consecutive same-side fills raise `RoundTripPairingError`") is exactly the right scope for v0.5. When Phase 4 multi-symbol ships, this invariant becomes the design decision to revisit, not a bug to fix.

---

## 7. Open Risks / Things Not Validated

- **Phase 3.5+ multi-day round-trip P&L** is **not being pursued** (decision 2026-05-12); the acceptance test asserts a single pinned fill ($273.18 vs $273.24). The full round-trip P&L assertion gate has never fired against real multi-day QC data. Cause: QC free tier's minute-data trailing window (~90 days, per [QC forum](https://www.quantconnect.com/forum/discussion/19781/getting-data-with-free-plan/)) truncated the captured backtest to a single trading day, so QC never simulated an exit. Acknowledged in the authority doc § 7 and § 10 with full rationale — not a finding, a coverage limit by choice.
- **Live retraining hook** — not implemented; v0.5 is offline-only by design. No risk in v0.5; would need a new authority section if it ever lands.
- **Multi-prediction-set composition in one spec** — `StrategySpec._check_phase1_boundaries` enforces singleton. Not a gap.
- **Playwright UI inspection** — skipped because there's no ML-specific UI surface in v0.5. Build Alpha UI was validated 2026-05-07; nothing has changed in the user-facing strategy-runs flow that would warrant re-screenshotting.

---

## 8. Skill State Note

This pass ran outside the auto-research-tick skill's defined modes (which are baseline / Build Alpha validation / nightly). The user authorized an ad-hoc ML-feature-focused pass while sleeping; the skill's state machine wasn't extended. `state.json.mode` remains `build-alpha-validation-complete-awaiting-review` — `cursor` is updated to point at this report. If ad-hoc ML validation becomes a recurring need, propose a new mode (`ml-validation-pending` / `ml-validation-complete-awaiting-review`) under the skill rather than continuing to slip these in beside the state machine.
