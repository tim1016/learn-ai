---
id: VCR-0013
severity: P2
status: remediated
area: strategy-keys
canonical_file: Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts
reference: PRD §12.2
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "Co-resolves with VCR-0004: production endpoint and Vitest mocks both standardize on the module-name registry keys (e.g. `spy_ema_crossover`), so the test-suite mock shape now matches the real `GET /api/engine/strategies` response."
  - "Backend contract is enforced by `PythonDataService/tests/test_engine_strategies_endpoint.py::test_every_registered_strategy_can_be_imported_by_key` and `test_every_registered_class_name_resolves_against_its_module` — every registry key is import-tested against `app.engine.strategy.algorithms.{key}`."
  - "`broker-start-stop-card.component.ts:18-21` keeps `FALLBACK_STRATEGY = 'spy_ema_crossover'` with the legacy-ledger justification comment (the only case the fallback fires is a ledger predating the strategy_key field)."
lens: strategy-registry-key-mapping
dedupe_with_F: none
confidence: high
---

## What

The Vitest mocks for `BrokerDeployFormComponent` and `BrokerInstancesComponent` set up `getEngineStrategies()` to return `[{name: 'spy_ema_crossover', ...}, {name: 'deployment_validation', ...}, {name: 'ema_crossover_options', ...}]`. The real production endpoint (`GET /api/engine/strategies`) returns `{name: 'ema_crossover', ...}` (registry key, not module name — see VCR-0004). Tests assert `expect(req.strategy_key).toBe('spy_ema_crossover')`, which is the form of the value that would survive end-to-end (since the runner needs the module name), but the form would never produce that value in production.

The test suite therefore gives a green build for a `strategy_key` value that production literally never emits. The downstream `FALLBACK_STRATEGY = 'spy_ema_crossover'` constant in `broker-start-stop-card.component.ts:21` is consistent with the mock-shape but inconsistent with the API-shape. The mismatch masked VCR-0004 from any CI signal.

## Where

- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts:39-60` — mock data with module-name values.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts:136-175` — fillRequired sets `'spy_ema_crossover'`; deploy assertion expects same.
- `Frontend/src/app/components/broker/broker-start-stop-card/broker-start-stop-card.component.ts:18-21` — `const FALLBACK_STRATEGY = 'spy_ema_crossover';`
- `Frontend/src/app/components/broker/broker-instances/broker-instances.component.spec.ts:645` — `strategy_key: 'spy_ema_crossover'` mock.

## Why this severity

PRD §7 P2: "test gap that masks a P1." Tests pass while production fails on the same code path — a fresh ledger built by the real deploy flow today carries `strategy_key='ema_crossover'`, and no test asserts that any operator path actually succeeds with that value.

## Trading impact

Indirect — by masking VCR-0004, the test suite gave the team confidence the dropdown was wired correctly. The actual operator-deploy flow for 7 of 10 strategies will fail at start time.

## Reproduction

```bash
sed -n '39,60p' Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.spec.ts
grep -n 'FALLBACK_STRATEGY' Frontend/src/app/components/broker/broker-start-stop-card/broker-start-stop-card.component.ts
grep -n "_STRATEGY_REGISTRY: dict" PythonDataService/app/routers/engine.py
```

## Suggested resolution (NOT auto-applied)

Resolves jointly with VCR-0004. Once the canonical key form is chosen:

1. Regenerate the Vitest fixtures from the real backend response shape (or extract them to a shared TS file imported by both code and tests so the two stay in lockstep).
2. Add a contract test at the PythonDataService boundary: `set(s.name for s in /api/engine/strategies) == set(_STRATEGY_REGISTRY.keys())`.
3. Update `FALLBACK_STRATEGY` to the canonical form. Comment why the fallback exists (currently masks legacy ledgers without `strategy_key`).

## Provenance of the finding

Lens: `strategy-registry-key-mapping` (workflow `wf_def78013-ce4`, structured-finding `frontend-mocks-mask-registry-mismatch`, verified 1/1 by adversarial pass). All 5 cited claims confirmed by direct read.
