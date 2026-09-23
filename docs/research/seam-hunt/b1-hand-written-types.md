# B1: hand-written frontend REST types vs the Python models they mirror

Research ticket: #2295, part of map #2276. Baseline: `a14f1df1` (master, 2026-09-23). All `file:line` citations are at that SHA.

## Answer

**No field has drifted.** I checked every hand-written type in `Frontend/src/app/api/` that has a live Python producer. Each one matches its Pydantic model field by field: nullability, enum members, numeric types and timestamp typing. The only differences are expected ones:

- Defaulted fields show as optional in the OpenAPI.
- openapi-typescript renders `dict[str, Any]` as `Record<string, never>`.
- `RunVerdict` deliberately makes some fields optional so persisted v1 verdicts still load.

Every timestamp is `*_ms: number`, and no field is typed as a string timestamp.

**The guards are weaker than the file names suggest.** Neither `*.contract.spec.ts` nor the `*.snapshot.spec.ts` in this directory reads any Python-owned artifact. Both check the frontend against hand-typed copies of itself.

What actually protects the money-path types is incidental. The operator blocker and posture values reach their components through **generated** types (`PanelAction`, `ClerkStatus`), so `tsc` checks, in one direction, that the wire shape can be assigned to the hand type. The Clerk transaction history has no such path, because it is fetched as `http.get<HandType>`. Nothing, at compile time or at runtime, would catch its drift.

Three of the seven files, plus one interface, have **no live producer or no live consumer**. They are dead mirrors.

Seam status: **cleared (static) for drift today; charted hazard for guard coverage** (see G1 and G2).

## Method

1. For each file, I found the Pydantic source and the matching `components['schemas'][...]` entry in the generated `broker.types.ts`. That file is produced from `contracts/openapi/python-data-service.openapi.json`, which is CI-gated. I ran the host venv to confirm that the live model's property set equals the snapshot (`OperatorBlocker`, `OperatorMove`, `AccountOperatorPosture`). `DeployPreflightResponse`, `OperatorNotice` and `OperatorIncident` are **not** in the OpenAPI at all.
2. I wrote a throwaway TypeScript check. It lived in `$TMPDIR` and was not committed. It builds `Sent<W>`, the generated type with every key required (Pydantic serializes defaulted fields, and no route in `app/` uses `response_model_exclude_none` or `exclude_unset`). It then classifies each property of `Sent<W>` against `Sent<Hand>` as `ok / hand-wider / hand-narrower / incompatible / wire-only / hand-only`. The pairs covered are 28 models from `operator-blocker`, `run-verdict`, `action-plan` and `clerk-transaction-history`. Result: zero `hand-narrower`, `incompatible`, `wire-only` or `hand-only` keys. The only `hand-wider` keys are `RunVerdict.parity_signature` and the `receipt` / `events` / `rows` dicts, and all of them come from the `Record<string, never>` artifact.
3. For the types that have no OpenAPI counterpart, I diffed the TS file against the Pydantic source by reading both.

## (a) Per-file drift table

| TS file | Python source of truth | On a live route? | Live TS consumer? | Field drift | Notes |
|---|---|---|---|---|---|
| `operator-blocker.types.ts` | `app/schemas/operator_blocker.py` | Yes, inside `PanelAction.blockers` and `ClerkStatus.operator_posture` (`broker.types.ts:11022`) | Yes: panel buttons, operator readiness, Alpaca operator posture and lens | **None**. All 8 anchor kinds, 4 dispositions, 4 hosts, 6 scopes and 3 audiences match (`operator_blocker.py:14-32`). The `fragment? / target? / confirmation? / required_token? / detail? / primary_move? / secondary_moves? / evidence?` optionals match Pydantic defaults (`operator_blocker.py:70,122,130-131,142,161-163`) | `DeployPreflightResponse` (`operator-blocker.types.ts:223`) mirrors `operator_blocker.py:213`, but no route returns it and no TS code imports it |
| `operator-notice.types.ts` | `app/operator/notices/schema.py` | **No.** `OperatorNotice` and `OperatorIncident` are not in the OpenAPI. Their only producers are `app/operator/incidents/{store,safety_halt_notices}.py`, which nothing outside `app/operator/` imports | **No.** `app-operator-notice` (`components/operator-notice/operator-notice.component.ts:15`) is mounted in no template | **None**. All 32 codes are in the same order (`schema.py:22-63` = `operator-notice.types.ts:33-65`). Tier, actionability, remedy status and the 6 action kinds match. `occurred_at_ms`, `started_at_ms` and `resolved_at_ms` are `number` | Dead mirror of a Python-internal shape |
| `run-verdict.types.ts` | `app/schemas/run_verdict.py` (OpenAPI `RunVerdict*`) | Yes (research runs) | Yes: Strategy Lab and the evidence grade | **None**. Every Literal matches. The fields TS marks optional (`status`, `evidence_action`, `missing_required_*`, `*_required_metrics`, `parity_signature`) are optional on purpose, for persisted v1 verdicts (`run-verdict.types.ts:49-68`) | Research surface, off the money path. `contracts/run-verdict-v1/fixture.json` is read only by the Python test `tests/services/test_run_verdict_parity.py` |
| `action-plan.types.ts` | `app/schemas/action_plan.py` (OpenAPI `ActionPlan`, legs, selectors) | Yes | **No.** Its only importer is `action-plan-format.ts`, and `action-plan-format.ts` has no non-spec importer | **None**. `expiration_ms: number` | Dead mirror |
| `clerk-transaction-history.types.ts` | `app/schemas` models returned by `app/routers/clerk_transactions.py:30-33,62,129-132` (OpenAPI `ClerkTransaction*`, `ClerkCustody*`, `ExternalOrderAcknowledgementResponse`) | Yes | Yes: the Account desk transaction history, the evidence drawer and the custody timeline | **None**. `feed_state` has 7 members on both sides. `transaction_origin` has 9. `broker`, `commission_status` and `fee_fidelity` match. Every one of the 12 `*_at_ms` fields and 6 `*_ms` durations is `number \| null` on both sides. The request query names (`brokers.service.ts:442-448`) match the route parameters (`clerk_transactions.py:65-73`) | **Unguarded**: fetched as `http.get<ClerkTransactionHistoryResponse>` / `http.get<ClerkTransactionDetail>` / `http.post<ExternalOrderAcknowledgement>` (`brokers.service.ts:451,464,476`) |
| `bot-lifecycle-chart.types.ts` | None. The Python source (`only_fresh_run_available` in `app/schemas/live_runs.py`) was deleted in `69bbeabe` (2026-08-07), and the projection runtime was retired in `55190a73` | No | **No.** Zero importers | n/a (orphan) | Dead mirror |
| `json-imported.ts` | n/a: a type helper, not a mirror | n/a | `components/examples/alpaca-bot-control/alpaca-bot-control-fixtures.ts:19-40` | n/a | It widens literals by design. Structure and nullability are still checked against the **generated** `AlpacaBotControlFixtureEnvelope` via `satisfies`. Enum-member drift inside those fixtures is intentionally not checked |
| `broker-models.ts` | SSE payloads | n/a | n/a | out of scope | Hand-mirrored SSE; covered by A1 (#2289: "payload types match Python exactly") |

Cross-stack string constants in `operator-blocker.types.ts` currently match Python:

- `ACCOUNT_DESK_CLERK_RECOVERY_ANCHOR = 'account-desk-clerk-recovery'` (`:166`) = `app/broker/alpaca/clerk/sqlite/account_operator_posture.py:49`
- `BOT_COCKPIT_RECONCILE_ANCHOR` (`:174`) = `app/services/broker_v2_panel/sqlite_panel_adapter.py:485`
- `BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR` (`:183`) = `sqlite_panel_adapter.py:488`

## (b) Which drifts could change what the operator sees or can do

Ranked by how close each one gets to the money path. None of them is drifted today; this is the blast radius if one did.

1. **Anchor constants (blocker moves).** `panelActionForMove` (`components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts:70-95`) maps a `confirm_in_form` anchor to `reconcile_now` or `prepare_safe_flatten`, and returns `null` for an anchor it does not recognize. The Alpaca operator lens opens its Clerk recovery panel only when the anchor matches (`components/brokers/alpaca-desk/alpaca-operator-lens.component.ts:79-82`). If Python renamed an anchor, all tests would pass, and the blocker would lose its in-place cure: the button disappears, which fails closed. The operator would see a blocker with no cure attached.
2. **`OperatorBlocker` enum widening in Python.** Suppose Python adds a new `Disposition`, a new `OperatorAction.kind`, or a new severity. OpenAPI regeneration changes `broker.types.ts`, and `tsc` then fails where the generated `PanelAction.blockers` or `ClerkStatus.operator_posture` is assigned into the hand type. So this drift **is** caught at build time, but only by accident, and only because no consumer casts (there is no `as OperatorBlocker` anywhere in non-spec code).
3. **Clerk transaction history.** `presentFeed` (`components/broker/account-desk/account-desk-transaction-history.component.ts:439-483`) has an exhaustive switch over the hand `feed_state` union and no default branch. If Python added a new feed state, the function would return `undefined` at runtime for the value called at `:202`. The desk's "is this history current / delayed / corrupt" header would then be blank, or would throw. It feeds custody evidence (fills, fees, lifecycle), not order placement.
4. `run-verdict`, `action-plan`, `bot-lifecycle-chart` and `operator-notice` have no money-path consumer.

## (c) Guard analysis

| Spec | What it compares | Pins against Python? |
|---|---|---|
| `api/operator-blocker.contract.spec.ts` | A hand-typed `CONTRACT_BLOCKER` literal against a hand-written key list, and `OPERATOR_BLOCKER_ANCHOR_KINDS` against a hand-written array (`:13-59`). It also tests the helper functions | **No.** It is self-referential. Adding or renaming a field or anchor kind in Python leaves it green |
| `api/operator-notice-codes.snapshot.spec.ts` | Two arrays, both inlined by hand (`:16-49`, `:51-84`) | **No.** Its header claims that JSON outside the project root cannot be imported (`:1-8`). That is false at HEAD: `tsconfig.json:22` maps `@repo-contracts/*`, `alpaca-bot-control-fixtures.ts:3-17` imports from it, and `broker-contracts.spec.ts:2` imports `contracts/fixtures/*.json` by relative path. The header also states `schema_version: 2` at `:11` but "schema_version 1" at `:15`. Typing the TS array `readonly OperatorNoticeCode[]` proves the array is a subset of the union, not that they are equal. The Python half (`tests/operator/test_notice_codes_snapshot.py:12-27`) guards only Python against `snapshot.json`, and its failure message points at the wrong TS file (`models/operator-notice.ts`, which is a re-export) |
| `api/alpaca-bot-control.contract.spec.ts` | 15 Python-authored fixtures under `contracts/fixtures/alpaca-bot-control/v1` `satisfies` the **generated** envelope type, checked at compile time (`alpaca-bot-control-fixtures.ts:19-40`) | **Yes** for structure and nullability, through OpenAPI. Enum members are deliberately widened by `JsonImported` |
| `api/broker-contracts.spec.ts` | `contracts/fixtures/data-plane-health-v1.json` against the generated `DataPlaneHealth` | Yes, but for a generated type, not a hand-written one |
| run-verdict / action-plan / clerk-transaction-history / bot-lifecycle-chart | No spec | No |
| Incidental: `tsc` on the component bindings | Generated `PanelAction.blockers` and `ClerkStatus.operator_posture` flow into the hand `OperatorBlocker` / `AccountOperatorPosture` | **One direction only** (can wire be assigned to hand). It does not cover the Clerk transaction history, which is a typed `HttpClient` generic |

## (d) Named suspected gaps

**G1: Clerk transaction history can drift with no guard (P2).**
Hypothesis: if Python adds a `feed_state` member (or renames or removes a field) in the `ClerkTransaction*` models and OpenAPI is regenerated, CI stays green. At runtime `presentFeed` then returns `undefined`, and the Account desk's history status header is blank or throws. A renamed field such as `execution_price` renders as "No execution recorded" or "N filled" (`account-desk-transaction-history.component.ts:430-436`).
Why P2: this is diagnostic evidence of custody, not an order path.
Prototype sketch:
1. In a worktree, add `"paused"` to the `feed_state` Literal.
2. Run `npm run codegen:openapi`, then `ng build` / `ng test`, and confirm they stay green.
3. Render the component with the new value and assert the header is empty or an error is thrown.

The candidate guard (not to be built in the prototype) is a type-level spec that asserts `Sent<components['schemas'][X]>` and the hand type are mutually assignable.

**G2: Blocker-move anchor strings have no cross-stack pin (P2).**
Hypothesis: if `BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR`, `BOT_COCKPIT_RECONCILE_ANCHOR` or `ACCOUNT_DESK_RECOVERY_ANCHOR` is renamed in Python, every Python and TS test stays green. `panelActionForMove` then returns `null`, and the blocker renders with no cure move. On the Alpaca desk, the in-place Clerk recovery panel becomes unreachable from its blocker.
Why P2: it fails closed, since no wrong action is dispatched. The cures also stay reachable by another route. The Clerk recovery panel is a `<details>` block the operator can open by hand (`alpaca-operator-lens.component.html:35`). The cockpit's `reconcile_now` and `prepare_safe_flatten` remain panel actions, and `panelActionForMove` only looks those actions up; it does not create them. So the operator loses the shortcut, not the capability.
Prototype sketch:
1. Change the Python constant.
2. Run `pytest tests/services/broker_v2_panel tests/broker/alpaca/clerk` and the lens and operator-lens specs, and confirm they stay green.
3. Render the lens with a blocker built from the new anchor, and assert that no move or recovery entry point exists.

The candidate guard is a `contracts/` JSON of anchor strings, read by both a Python test and a TS spec.

**G3: The operator-notice snapshot spec is self-pinning, and its excuse is stale (P3).**
Hypothesis: if a Python notice code is added and `snapshot.json` is updated, the TS union is left stale and CI stays green.
Why P3: there is no consequence today, because `OperatorNotice` is on no route and `app-operator-notice` is mounted nowhere.
Prototype sketch: add a code on the Python side only, and run both suites.

**G4: `operator-blocker.contract.spec.ts` is labelled "contract" but pins the frontend against itself (P3).**
Hypothesis: if a hand type gains a field or enum member that Python never sends, CI stays green.
Why P3: the incidental `tsc` check covers the dangerous direction (wire into hand) today.
Prototype sketch: add `disposition: 'escalate'` to the TS type only, and confirm that CI stays green.

**G5: Dead mirrors and unused contract helpers (P3).**
- `bot-lifecycle-chart.types.ts` (its Python source was deleted in `69bbeabe`).
- `action-plan.types.ts` and `action-plan-format.ts` (no non-spec consumer).
- `DeployPreflightResponse` (`operator-blocker.types.ts:223`; no route and no consumer).
- `operator-notice.types.ts`, `models/operator-notice*.ts` and `components/operator-notice/` (unmounted).
- `accountDeskAnchorOrVerdictFallback`, `operatorBlockersForAccountDeskLens`, `operatorAttentionConditionCount` and `OPERATOR_BLOCKER_ANCHOR_KINDS` are used only by specs.

As a result, no host reads `OperatorBlocker.anchor` or `.audience`. The "future anchor kinds collapse to the verdict card" protection (`operator-blocker.types.ts:44-64`) is not wired into anything.
Hypothesis: the backend's `audience` routing is ignored, so trader and operator lenses show the same guidance.
Prototype sketch: render the Alpaca posture with an `audience: 'operator'` blocker in the trader lens.

**G6: Frontend-authored feed prose (P3).**
`presentFeed` writes its own headline and detail per `feed_state` (`:439-483`), and also renders the backend's `feed_headline` / `feed_detail` (`.html:183`). The two sets of copy can disagree. This is imprecise, not wrong.

## (e) Defects proven by reading

All of these are **P3**. There is no proven P0 or P1, so no `bug` issues were filed.

- **P3-1.** `operator-notice-codes.snapshot.spec.ts:1-8` claims JSON outside the project root cannot be imported. That claim is disproved at HEAD (`tsconfig.json:22`, `alpaca-bot-control-fixtures.ts:3`, `broker-contracts.spec.ts:2`), and it is the reason the spec pins nothing. The spec also contradicts itself on the schema version (`:11` says 2, `:15` says 1).
- **P3-2.** `tests/operator/test_notice_codes_snapshot.py:26` tells the developer to update `Frontend/src/app/models/operator-notice.ts`. The literal actually lives in `Frontend/src/app/api/operator-notice.types.ts:33`.
- **P3-3.** Dead mirrors, as listed in G5.
