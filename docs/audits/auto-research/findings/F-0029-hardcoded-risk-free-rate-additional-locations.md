---
id: F-0029
severity: P2
status: open
area: inventory
canonical_file: PythonDataService/app/models/{strategy,portfolio}.py; app/research/options/{iv_builder,contract_finder}.py
reference: docs/math-sources-of-truth.md (Risk-free rate row + Known rule-5 non-compliance item 5)
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 2
---

## What

Phase 2 verification of registry's "Known rule-5 non-compliance" item 5 (hardcoded `r = 0.043`). The registry says: *"in `app/research/options/iv_builder.py::DEFAULT_RISK_FREE_RATE`, `app/research/options/contract_finder.py::DEFAULT_RFR`, and Pydantic field defaults in `app/models/{strategy,portfolio}.py`"* — implying ~4 locations.

Actual count is **5+ production constants** plus the canonical `FALLBACK_RATE` in `fred_service.py`:

| File | Line | Constant |
|---|---|---|
| `app/services/fred_service.py` | 32 | `FALLBACK_RATE = 0.043` (canonical fallback — fine) |
| `app/research/options/iv_builder.py` | 18 | `DEFAULT_RISK_FREE_RATE = 0.043` |
| `app/research/options/contract_finder.py` | 26 | `DEFAULT_RFR = 0.043` |
| `app/models/strategy.py` | 48 | `risk_free_rate: float = Field(0.043, ge=0, le=0.5)` |
| `app/models/portfolio.py` | 97 | `risk_free_rate: float = Field(0.043, ge=0, le=0.5)` |
| `app/models/portfolio.py` | 184 | `risk_free_rate: float = Field(0.043, ge=0, le=0.5)` (second model) |

Plus references in routers (`app/routers/snapshot.py:86` comment) and dividend services. The registry undercount (~4 vs 6) is itself a tracking drift.

## Why this severity

P2 — Registry knows the conceptual issue; the count is just slightly off. None of these are user-facing wrong numbers — they're defaults that callers can override. The fix is "use `fred_service.get_rate()` instead of literal", which the registry already prescribes.

## Reproduction

```
grep -rnE 'r\s*=\s*0\.043|risk_free_rate.*0\.043|0\.043.*risk_free|DEFAULT_RISK_FREE_RATE|DEFAULT_RFR|FALLBACK_RATE' PythonDataService/app/
```

## Suggested resolution (NOT auto-applied)

Update `math-sources-of-truth.md` § "Known rule-5 non-compliance" item 5 to:

> 5. **Hardcoded `r = 0.043`** in **6 production locations** (counting both `Field(0.043, ...)` defaults in `app/models/portfolio.py` lines 97 and 184): `iv_builder.py:18`, `contract_finder.py:26`, `strategy.py:48`, `portfolio.py:97, :184`. `fred_service.py:32 FALLBACK_RATE = 0.043` is the canonical fallback and is correct. **Migration: deferred** — full migration to FRED-interpolated rates per `docs/math-rigor.md` Upgrade 4.

The actual remediation (route every caller through `fred_service.get_rate()`) is the deferred Phase work, unchanged.

## Provenance of the finding itself

Phase 2 / cursor: targeted grep of `0.043` and `DEFAULT_R*FR*` patterns across `PythonDataService/`. Cross-checked against registry's enumeration.
