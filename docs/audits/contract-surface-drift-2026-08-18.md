# Contract-surface drift and gate-teeth audit — 2026-08-18

> **STATUS: SUPPORTING POINT-IN-TIME EVIDENCE — NOT IMPLEMENTATION AUTHORITY.**
> This is a diagnosis at the pinned commit. ADRs and current code remain the
> authorities; `docs/known-gaps.md` remains the defect register.

**Charter:** [#1646](https://github.com/tim1016/learn-ai/issues/1646)

**Commit read:** [`a16571c2736bc3213e648ba6960aa951b6177b9b`](https://github.com/tim1016/learn-ai/tree/a16571c2736bc3213e648ba6960aa951b6177b9b)

**Question:** Do the committed OpenAPI, GraphQL, broker-v2 vocabulary, operator
manual, and ADR metadata contracts match their sources, and can their CI gates
actually fail when those sources drift? Do the live broker consumers use the
generated client the gates protect?

**Answer:** The committed generated artifacts are current, and the main
regenerate-and-diff gates have teeth. The production FastAPI schema and committed
OpenAPI document were exactly equal across the canonical Alpaca Broker V2
`/api/brokers/**` slice: 46 path templates and 49 HTTP operations. Regenerating
GraphQL, both vocabulary snapshots, both operator-manual copies, and both
Frontend clients produced no tracked diff. The focused consumer checks passed.

Three holes remain:

1. The two broker-v2 vocabulary snapshots are not regenerated in CI and neither
   contract test compares snapshot copy with the live `OPERATOR_COPY`. Both
   snapshots can contain the same wrong, nonempty label while the Python and
   Frontend contract tests remain green.
2. Eleven canonical Broker V2 REST schemas already exist in generated
   `broker.types.ts`, but their chart, evidence, and gallery consumers redeclare
   them as handwritten interfaces. The generated client can be current while the
   live consumer reads a separate mirror.
3. The ADR guard enforces `Status` exactly, but it does not enforce ADR 0040's
   forward-only `Vocabulary:` declaration. An accepted, governed ADR can lose the
   declaration while the gate stays green.

The charter's .NET premise needs correction rather than a defect: no canonical
Alpaca Broker V2 request travels through .NET, no C# OpenAPI client generator
exists, and the GraphQL schema exposes no Broker V2 control field. The current
`/api/brokers/**` surface is browser-to-FastAPI direct. The handwritten C# clients
serve non-broker Python routes and are outside this charter.

## Scope and method

This audit used repository-owned primary evidence only: workflow definitions,
generators, exported schemas, Pydantic/Hot Chocolate sources, generated TypeScript,
and the consumers themselves. No production broker, account, credential, database,
or live order path was contacted.

Per the repository's deprecation rule, the audit scope is only the canonical
Alpaca Broker V2 `/api/brokers/**` family. Deprecated compatibility families,
including singular `/api/broker/**` and `/api/live-instances/**`, and their IBKR
bot-control consumers were excluded from inventory and analysis. They are named
here only to make that exclusion explicit.

Each deliberate drift was minimal, local, and uncommitted. For every claimed
gate, the audit changed the source or artifact the gate claims to protect, ran the
same command CI runs (or the exact focused contract test selected by CI), recorded
red or green, reverted the mutation, regenerated the artifact, and required the
working tree to return clean. A green result under deliberate drift is a finding.

The FastAPI “live” surface in this document means `app.openapi()` with the export
command's production settings, including fault injection forced off. It is the
same schema served by FastAPI's OpenAPI endpoint; no network request is needed to
compare it. Streaming event bodies are outside OpenAPI by protocol. A REST
bootstrap model reused as an SSE snapshot is still OpenAPI-owned for the REST
half; a stream-only incremental event remains a named handwritten exception.

## Canonical boundary topology

Four FastAPI routers own the canonical route family under the same
`/api/brokers` prefix: `brokers-v2`, `broker-bots`, `broker-v2-panel`, and
`broker-v2-gallery`. The read router states that phase 1 registers only `alpaca`
([`brokers.py:1-8,108`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/app/routers/brokers.py#L1-L108)),
and application wiring identifies the latter three control surfaces as Alpaca
Broker V2
([`main.py:701-730`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/app/main.py#L701-L730)).

The committed OpenAPI slice contains **46 path templates / 49 operations**:
22 `brokers-v2`, 6 `broker-bots`, 19 `broker-v2-panel`, and 2
`broker-v2-gallery` operations. The live schema produced the same paths and
operations. Production Frontend source has exactly three direct HTTP consumers
of this route family: `brokers.service.ts`, `broker-v2-panel.service.ts`, and
`gallery-live-store.service.ts`. Their base URLs are visible at
[`brokers.service.ts:49-57`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/services/brokers.service.ts#L49-L57),
[`broker-v2-panel.service.ts:38-52`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.service.ts#L38-L52),
and
[`gallery-live-store.service.ts:338`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts#L338).
The proxy sends those `/api` requests directly to PythonDataService
([`Frontend/proxy.conf.js:149-165`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/proxy.conf.js#L149-L165)).

The regenerated GraphQL snapshot contains no Broker V2 control query or mutation,
and a repository search found **zero** C# calls to `/api/brokers`. `Backend.csproj`
has Hot Chocolate and ordinary `HttpClient` dependencies but no
`OpenApiReference`, NSwag, Kiota, or other C# generator
([`Backend.csproj:13-27`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Backend/Backend.csproj#L13-L27)).
The two existing C# cross-stack fixture tests cover aggregates and spec-strategy
payloads, not Alpaca Broker V2
([`CrossStackContractFixtureTests.cs:16-45`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Backend.Tests/Contract/CrossStackContractFixtureTests.cs#L16-L45)).

Therefore:

- **The three direct Frontend consumers and their generated client are in scope
  and were checked.**
- **A .NET Alpaca Broker V2 client is not stale; it does not exist.** No issue is
  owed for this boundary on that premise.
- The broader question of generating every non-broker C# Python client is not
  silently promoted into this charter.

## Current regeneration result

All source-to-artifact regenerations were clean after the deliberate probes were
removed:

| Artifact | Authoritative source | Regeneration / check | Result |
|---|---|---|---|
| FastAPI OpenAPI | `app.main.app.openapi()` | `python PythonDataService/scripts/export_openapi_contract.py` and `--check` | **Clean.** The canonical `/api/brokers/**` slice was identical: 46 path templates / 49 operations on both sides (22 `brokers-v2`, 6 `broker-bots`, 19 `broker-v2-panel`, 2 `broker-v2-gallery`). |
| Backend GraphQL | Hot Chocolate schema | `dotnet run --project Backend --configuration Release -- schema export --output ../contracts/graphql/backend.schema.graphql` | **Clean.** No diff. The relative output path is correctly resolved from the project directory; it was not changed. |
| Broker-v2 vocabulary snapshots | `ALL_VOCABULARY_CODES` + `OPERATOR_COPY` | `(cd PythonDataService && python -m scripts.regenerate_broker_v2_vocabulary_snapshot)` | **Clean now.** Both copies regenerated byte-identically, but CI does not run this command; see F1. |
| Broker-v2 operator manual | `OPERATOR_COPY`, action registry, recovery action vocabulary | `(cd PythonDataService && python -m scripts.regenerate_broker_v2_operator_manual)` | **Clean.** Canonical and served manuals are byte-identical. |
| Frontend GraphQL client | committed GraphQL schema + `.graphql` operations | `npm --prefix Frontend run codegen:graphql` | **Clean.** No generated diff. |
| Frontend OpenAPI client | committed FastAPI OpenAPI snapshot | `npm --prefix Frontend run codegen:openapi` | **Clean.** No `broker.types.ts` diff. |

The OpenAPI exporter compares deterministic schema text directly
([`export_openapi_contract.py:42-57,79-101`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/scripts/export_openapi_contract.py#L42-L101)).
The GraphQL job regenerates then diffs the committed path
([`ci.yml:115-127`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/.github/workflows/ci.yml#L115-L127)).
The Frontend check regenerates both client outputs and diffs only those outputs
([`Frontend/package.json:16-19`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/package.json#L16-L19)).

## Adversarial gate results

| Claimed protection | Deliberate local drift | Observed result | Verdict |
|---|---|---|---|
| Committed OpenAPI matches the FastAPI broker surface | Added optional `contract_drift_probe` to the live `BrokerAccountSnapshot` model without regenerating the contract. | `export_openapi_contract.py --check` exited 1 and showed the missing `BrokerAccountSnapshot` property. | **TOOTHFUL.** Source drift and artifact tampering both change the direct comparison. |
| Committed GraphQL matches Hot Chocolate | Added `contractDriftProbe` to `BacktestRunNodeType` without updating the snapshot. | Exact CI export plus `git diff --exit-code` exited 1 and showed the new schema field. | **TOOTHFUL.** |
| Frontend GraphQL generated client is current | Added `__typename` to the committed spec-strategy operation without regenerating outputs. | `npm --prefix Frontend run codegen:check` exited 1 with diffs in `generated/gql.ts` and `generated/graphql.ts`. | **TOOTHFUL for generated operations.** Legacy handwritten `gql` documents remain the already-documented exception. |
| Frontend OpenAPI generated client is current | Added `contract_drift_probe` to the committed `BrokerAccountSnapshot` OpenAPI schema without regenerating `broker.types.ts`. | `npm --prefix Frontend run codegen:check` exited 1 and showed the missing generated property. | **TOOTHFUL for the generated file.** F2 is a consumer bypass, not a generator failure. |
| Broker-v2 vocabulary source stays aligned with code snapshot | Added `contract_drift_probe` to `ActionId` and `ACTION_IDS` without copy or snapshot. | CI's filename matcher selected `test_vocabulary_snapshot.py`; the focused test failed twice: missing from snapshot and missing `OPERATOR_COPY`. | **TOOTHFUL for `vocabulary.py` membership drift.** This depends on diff-driven discovery. |
| Broker-v2 vocabulary snapshot content matches the source | Changed `resume.label` to the same wrong, nonempty text in both committed snapshot copies. | The copies remained byte-identical. Python: **17 passed**. Frontend copy contract: **6 passed**. CI's changed-path selector produced `extra=<none>` for the JSON-only diff. | **TOOTHLESS — F1.** Neither test compares snapshot `copy` to `OPERATOR_COPY`, and CI never regenerates the snapshots. |
| Generated operator manual matches backend copy | Changed the live `resume` label in `OPERATOR_COPY` without updating either manual. | The manual generator rewrote both manuals; the exact CI diff exited 1 on the changed row in both files. | **TOOTHFUL.** |
| ADR status is canonical | Changed ADR 0041 from `Accepted` to `Shipped`. | `check_adr_status.py` exited 1 with `malformed-status`. | **TOOTHFUL for status.** |
| Accepted ADR carries its forward-only `Vocabulary:` declaration | Removed ADR 0041's declaration while leaving canonical `Accepted`. | `check_adr_status.py` exited 0: `ADR status guard passed (41 ADRs).` | **TOOTHLESS — F3.** |

The operator-manual job is a real regenerate-and-diff gate over both rendered
copies
([`ci.yml:183-205`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/.github/workflows/ci.yml#L183-L205)).
By contrast, the Python fast baseline excludes `tests/broker`; the conditional
selector runs a broker test only for a matching changed Python filename
([`ci.yml:207-215,231-304`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/.github/workflows/ci.yml#L207-L304)).

## Confirmed findings

### F1 — Vocabulary snapshot prose can drift while every contract test stays green

The snapshot generator explicitly says it writes two identical files from
`ALL_VOCABULARY_CODES` and `OPERATOR_COPY`, and that drift from either direction
surfaces
([`regenerate_broker_v2_vocabulary_snapshot.py:1-28`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py#L1-L28)).
The implementation does generate the exact live label and explanation for every
code
([`lines 82-115`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/scripts/regenerate_broker_v2_vocabulary_snapshot.py#L82-L115)).
The command simply is not wired into CI.

The Python test compares only the snapshot's **code set** with the live set
([`test_vocabulary_snapshot.py:57-65`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py#L57-L65)).
Its copy test reads `OPERATOR_COPY` directly, not `snapshot["copy"]`
([`lines 84-98`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/tests/broker/v2panel/test_vocabulary_snapshot.py#L84-L98)).
The Frontend test proves snapshot copy is nonempty and fallback keys are covered;
it has no import path to the Python authority. Therefore identical, plausible,
wrong prose in both JSON snapshots passes both sides. This is exactly the
“regenerates or compares against the wrong authority” failure shape the charter
asked to test.

Severity: **medium**. The backend still sends current copy at runtime and the
operator manual has its own working generator, so this is not a present actuation
failure. It is a false contract claim and a stale emergency/documentary artifact
path.

### F2 — Eleven live Broker V2 REST contracts bypass the generated client

ADR 0031 permits handwritten Frontend contracts only as an explained refinement
over a generated type or as a transport shape OpenAPI cannot describe. The
canonical panel mostly follows that rule: `BotPanelLiveSnapshot`, panel views,
actions, and blocker types are aliases of `components['schemas']`
([`broker-v2-panel.types.ts:44-68,82-88,118-121,180`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.types.ts#L44-L180)).

The same file nevertheless redeclares seven generated REST schemas field by
field: `ChartBar`, `ChartFillMarker`, `ChartOverlayNoticeView`,
`ChartLiveResponse`, `ChartHistoryResponse`, `EvidenceEntry`, and `EvidencePage`
([`lines 123-212`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/lib/broker-v2-panel.types.ts#L123-L212)).
The gallery adds four more: `GalleryPrimaryAction`, `GalleryBotView`,
`GallerySymbolBars`, and `GalleryLiveSnapshot`
([`gallery.types.ts:18-60`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery.types.ts#L18-L60)).
All eleven names already exist in generated `broker.types.ts`.

The gallery snapshot is not an SSE-only exception: the same Pydantic model is
the REST bootstrap response and the SSE `snapshot` event
([`broker_v2_gallery.py:85-96`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/app/schemas/broker_v2_gallery.py#L85-L96)).
The live store imports the handwritten mirror for both paths
([`gallery-live-store.service.ts:10-17,30-42`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/Frontend/src/app/components/broker/v2-panel/gallery/lib/gallery-live-store.service.ts#L10-L42)).
Only `GalleryLiveUpdate` and `GalleryResetEvent` are genuinely stream-only here.
`GalleryLiveUpdate` already has a canonical Pydantic owner at
[`broker_v2_gallery.py:99-109`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/app/schemas/broker_v2_gallery.py#L99-L109).
`GalleryResetEvent` does **not** have a Pydantic model: the router currently owns
its exact `{reason, cursor}` JSON shape inline
([`broker_v2_gallery.py:197-205`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/PythonDataService/app/routers/broker_v2_gallery.py#L197-L205)).

The current mirrors match the produced wire fields on manual read-through, so
this is not current contract drift. It is a gate hole: regenerating
`broker.types.ts` cannot update or typecheck a consumer that imports a different
interface. The final generated-client check, TypeScript compile, and four focused
broker contract tests all passed while the bypass remained.

Severity: **medium**. A later breaking Pydantic change can update OpenAPI and the
generated file in the same PR while the live chart/evidence/gallery consumer
continues compiling against stale local declarations.

### F3 — ADR 0040's `Vocabulary:` obligation is not enforced

The ADR status script is honest about its scope: it checks exactly one canonical
status line with one of four values
([`check_adr_status.py:1-20,32-44`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/scripts/check_adr_status.py#L1-L44)).
Its file checker never searches for `Vocabulary:`
([`lines 55-118`](https://github.com/tim1016/learn-ai/blob/a16571c2736bc3213e648ba6960aa951b6177b9b/scripts/check_adr_status.py#L55-L118)).

That is narrower than ADR 0040 Decision 4 and Consequence 7: every newly accepted
ADR must name the glossary section it added or explicitly state that none is owed,
and the obligation is supposed to become checkable. Removing ADR 0041's line was
therefore real governed drift, not an invented style rule, and the current guard
accepted it.

Severity: **low/medium**. This cannot corrupt a wire contract directly, but it
allows live domain vocabulary to become undiscoverable—the exact rot ADR 0040 was
accepted to stop.

## Refuted concerns and limits

- **OpenAPI is not comparing against its own output.** `--check` renders the live
  app schema in memory and compares it to the committed file without writing it.
- **GraphQL is not writing to the wrong path.** The charter's warning is correct:
  `../contracts/...` resolves from the Backend project directory. The deliberate
  schema field changed the committed repository snapshot and made the job red.
- **The operator-manual gate covers the file the app serves.** The source and
  Frontend asset both changed under the deliberate backend-copy mutation.
- **Frontend code generation is not green because it ignores generated output.**
  Both an operation change and an OpenAPI schema change produced a red diff. F2
  exists after that generation step, at the consumer-import choice.
- **No current generated artifact drift was found.** Every generator was run
  after the probes and the repository returned to a clean tracked state.
- **SSE is not declared fully protected by OpenAPI.** The stream-only update and
  reset envelopes remain handwritten in Frontend. The proposed F2 fix pins
  `GalleryLiveUpdate` to its existing Pydantic owner; for `GalleryResetEvent`, it
  either introduces a backend reset model or explicitly pins the router-owned
  `{reason, cursor}` shape with a focused boundary fixture.

## Recommended tracker items

The following filed issue bodies are independently grabbable.

### Filed issue 1 — [#1666](https://github.com/tim1016/learn-ai/issues/1666)

**Title:** Make broker-v2 vocabulary snapshots a regenerate-and-diff CI contract

**Body:**

> Charter #1646 proved that the broker-v2 vocabulary snapshots can carry the same
> wrong, nonempty operator copy while all existing contract tests stay green. At
> `a16571c2`, both JSON snapshots were deliberately changed so `resume.label`
> disagreed with live `OPERATOR_COPY`; the snapshots remained byte-identical,
> Python reported 17 passing vocabulary tests, Frontend reported 6 passing copy
> tests, and CI's diff-driven Python selector produced no extra test for the
> JSON-only change. The generator already produces the exact desired artifacts,
> but CI never runs it, and the Python test compares only code membership.
>
> Acceptance criteria:
>
> - Add a dedicated, unconditional broker-v2 vocabulary contract job (or an
>   equivalently unconditional step) with `working-directory: PythonDataService`
>   that runs
>   `python -m scripts.regenerate_broker_v2_vocabulary_snapshot` and then
>   `git diff --exit-code` over both committed snapshot paths.
> - Keep one backend authority: `ALL_VOCABULARY_CODES` plus `OPERATOR_COPY`.
>   Do not add a third hand-maintained list.
> - Assert exact snapshot `copy` equality with live label/explanation, not merely
>   nonempty values; assert the two committed copies remain byte-identical.
> - Regression-prove red for (a) source membership drift, (b) source copy drift,
>   (c) one-copy artifact tampering, and (d) identical wrong copy in both files;
>   prove green only after regeneration.
> - Correct the generator/test comments that currently claim either-direction
>   drift is already guaranteed to surface.
> - Do not change runtime operator copy or product behavior.

### Filed issue 2 — [#1667](https://github.com/tim1016/learn-ai/issues/1667)

**Title:** Make live Broker V2 REST consumers use generated OpenAPI types

**Body:**

> Charter #1646 found eleven live Broker V2 REST schemas that are present in
> generated `Frontend/src/app/api/broker.types.ts` but redeclared as handwritten
> interfaces in the actual consumers: `ChartBar`, `ChartFillMarker`,
> `ChartOverlayNoticeView`, `ChartLiveResponse`, `ChartHistoryResponse`,
> `EvidenceEntry`, `EvidencePage`, `GalleryPrimaryAction`, `GalleryBotView`,
> `GallerySymbolBars`, and `GalleryLiveSnapshot`. The gallery snapshot is both a
> REST bootstrap response and an SSE snapshot payload, so its REST half is not an
> OpenAPI exception. Current fields agree on read-through, but codegen and
> TypeScript can stay green after future schema regeneration because the live
> stores import the separate mirrors.
>
> Acceptance criteria:
>
> - Replace the eleven raw mirrors with direct aliases or explicitly documented
>   refinements of `components['schemas'][...]`; derive named literal unions from
>   generated fields rather than repeating their members.
> - Preserve readonly presentation ergonomics through a generic refinement if
>   needed, without restating wire keys or scalar types.
> - Make chart, evidence, and gallery REST calls consume those generated-backed
>   types, including the gallery REST bootstrap and its identical SSE snapshot.
> - Keep only true stream-only shapes handwritten. Pin `GalleryLiveUpdate` to
>   its existing backend owner,
>   `app.schemas.broker_v2_gallery.GalleryLiveUpdate`, with a focused parity
>   test. `GalleryResetEvent` has no existing Pydantic owner: either introduce a
>   backend reset model and pin Frontend to it, or explicitly designate the
>   router's exact `{reason, cursor}` JSON object as authority and add a focused
>   boundary fixture for that shape.
> - Add a regression demonstrating that a breaking field drift in one Pydantic
>   REST model, followed by normal OpenAPI/client regeneration, makes the
>   consuming Frontend check fail until the consumer is updated.
> - From `Frontend/`, run `npm run codegen:check`, `npx tsc --noEmit`, and
>   focused chart/evidence/gallery tests. No UI or product behavior change.

### Filed issue 3 — [#1668](https://github.com/tim1016/learn-ai/issues/1668)

**Title:** Enforce accepted-ADR Vocabulary metadata in the ADR guard

**Body:**

> Charter #1646 removed ADR 0041's required `Vocabulary:` declaration while
> leaving `**Status:** Accepted`; `python scripts/check_adr_status.py` still
> reported `ADR status guard passed (41 ADRs)`. Status syntax is protected, but
> ADR 0040 Decision 4's forward-only obligation is not: every newly accepted ADR
> must name its `CONTEXT.md` section or explicitly say that no vocabulary is owed.
> Existing older ADRs are intentionally not backfilled.
>
> Acceptance criteria:
>
> - Extend the ADR metadata guard (or add a sibling invoked by the same CI job)
>   so every governed `Accepted` ADR from ADR 0040 onward has exactly one
>   `Vocabulary:` metadata declaration.
> - Accept either a named `CONTEXT.md` target or an explicit “none owed” reason;
>   reject missing, empty, or duplicate declarations.
> - Preserve ADR 0040's forward-only rule: do not require a historical corpus
>   backfill and do not invent a declaration for ADR 0039.
> - Regression-prove that removing ADR 0041's line turns CI red while the current
>   corpus remains green.
> - Keep `Status` semantics and the four-value vocabulary unchanged.

## Exact `docs/known-gaps.md` addition

The registered section is reproduced here as the charter's point-in-time handoff.

> ## 8. Contract-surface drift gates (verified 2026-08-18)
>
> Source: `docs/audits/contract-surface-drift-2026-08-18.md`, read at commit
> `a16571c2`. OpenAPI, GraphQL, both Frontend generated clients, and the
> broker-v2 operator manual regenerated clean; deliberate drift proved those
> regenerate-and-diff gates can turn red.
>
> - **Broker-v2 vocabulary snapshot prose is not source-pinned (medium).** CI
>   does not run `regenerate_broker_v2_vocabulary_snapshot`, the Python contract
>   test compares only code membership, and the Frontend test checks only
>   nonempty copy/fallback coverage. Both committed snapshots can carry the same
>   wrong label or explanation while every current contract test remains green.
>   [#1666](https://github.com/tim1016/learn-ai/issues/1666)
> - **Eleven live Broker V2 REST types bypass generated OpenAPI aliases
>   (medium).** Chart, evidence, and gallery consumers hand-copy schemas already
>   present in `broker.types.ts`; the gallery snapshot is also a REST bootstrap,
>   not solely an SSE exception. Generated clients can be current while these
>   live consumers compile against stale mirrors. Keep true stream-only
>   envelopes handwritten: pin `GalleryLiveUpdate` to
>   `app.schemas.broker_v2_gallery.GalleryLiveUpdate`; for model-less
>   `GalleryResetEvent`, introduce a backend model or explicitly fixture-pin the
>   router-owned `{reason, cursor}` shape.
>   [#1667](https://github.com/tim1016/learn-ai/issues/1667)
> - **Accepted-ADR `Vocabulary:` metadata is not gated (low/medium).** The ADR
>   status guard correctly enforces status syntax and value but accepts a
>   governed accepted ADR after its ADR 0040 declaration is removed. Enforce the
>   forward-only rule without backfilling pre-0040 ADRs.
>   [#1668](https://github.com/tim1016/learn-ai/issues/1668)

## Verification evidence

Final clean-state checks after every deliberate mutation was removed:

- FastAPI live/committed canonical Alpaca Broker V2 equality: **46/46 path
  templates and 49/49 operations** under `/api/brokers/**`; three production
  Frontend direct consumers and zero C# direct consumers.
- OpenAPI exporter tests + vocabulary tests + manual-generator tests:
  **30 passed**.
- Frontend `(cd Frontend && npm run codegen:check && npx tsc --noEmit)`:
  **passed**.
- Frontend broker contract focus: **4 files, 11 tests passed**.
- Backend cross-stack contract fixtures: **2 passed**.
- GraphQL export and committed diff: **clean**.
- ADR status guard: **41 ADRs passed**.
- `git add --intent-to-add docs/audits/contract-surface-drift-2026-08-18.md`
  followed by `git diff --check`: **clean**, including this new audit file.

The .NET run also reported the repository's existing Hot Chocolate package
vulnerability warning and EF Core version-conflict warnings. They did not fail
the contract fixtures and were not caused or investigated by this docs-only
charter.
