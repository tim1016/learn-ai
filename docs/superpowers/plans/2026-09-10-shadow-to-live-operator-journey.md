# Shadow to Live operator journey — implementation handoff for Terra

**Status:** Proposed implementation plan; no application changes or broker operations have been performed by this plan.
**Prepared:** 2026-09-10 UTC, against `97992392`.
**Executor:** Terra. Execute the numbered tasks sequentially when implementation is requested. This document does not start another task or authorize real-account activation, arming, orders, resets, or service restarts.
**Evidence:** [workflow audit](../../audits/alpaca-paper-live-workflow-2026-09-09.md) and [open gaps, section 12](../../known-gaps.md).

## Outcome and scope

An operator can select a Shadow instance, inspect its comparison with a matching Paper twin, seal a qualifying rehearsal receipt, follow the existing offline account cutover, create a new Live instance from the reviewed configuration, and explicitly arm that instance with a verified link to its rehearsal. The Alpaca UI explains the current state, why a step is unavailable, and the next action. The server owns every verdict and permission.

Keep Shadow. Rehearsal is the selected route for this first graduation, while direct Live deployment remains supported under ADR 0059's optional-rehearsal policy. Paper, Shadow and Live continue to use the existing signal engine, semantic action plan, SQLite Clerk and account-specific effect ports.

This delivery includes separate Paper-twin evidence access, per-instance rehearsal progress, receipt creation, predecessor verification, Live preparation, supervised browser arming/disarming, and the existing guarded loss-hold release. Account activation stays an offline operation; its UI offers the exact procedure and observes its result. A browser page cannot stop its own server and honestly complete an offline cutover.

Defer the general Paper/Live credential-profile selector, simultaneous Shadow and Live in one process, reverse graduation, history pagination, new fill models, decision-bar comparison, and unrelated naming cleanup. The current two-context Paper/Shadow topology is sufficient for a first sequential graduation. If the actual account has already graduated or fails Shadow's history proof, report that concrete prerequisite; complete independent software work without deleting evidence to force entry into Shadow.

## Execution discipline

1. Read repository `AGENTS.md` and `docs/doc-authority.md`. Before the relevant task, read the Python, Angular, testing and temporal rules. Apply `add-fastapi-endpoint` and `build-angular-component` for their respective changes; apply `learn-ai-validation` if mathematical code is touched. No new numerical implementation is planned.
2. Work on a `codex/` branch or isolated checkout appropriate to the workspace. Preserve unrelated changes and the existing audit/backlog edits. Inspect the current commit: if the named source moved, find its callers and tests before adapting the plan.
3. Work on one numbered behavioral task at a time. For a defect, first obtain the stated failing observation, then make the smallest coherent fix, then run its consumer tests. Avoid a single change spanning the ledger, all transports and the entire UI.
4. Use explicit fake settings with `_env_file=None`, temporary artifact roots, a deterministic clock and fake broker ports. Existing developer `.env` files select Live; clearing process variables alone does not isolate Pydantic settings. Ordinary tests must never discover real credentials or runtime artifacts.
5. At each checkpoint record: files changed, failing case reproduced, passing tests, remaining limitation, next task. Continue authorized implementation after a checkpoint; a task boundary is not an automatic permission request.
6. Keep three completion claims separate: **software verified**, **real Shadow rehearsal completed**, and **Live execution verified**. An unavailable runtime blocks the latter two, not isolated implementation. Mocked browser tests cannot establish real broker readiness.

## Design decisions for this implementation

These are concrete proposed choices for the implementation review, not amendments already accepted by virtue of this document.

| Concern | Planned decision |
|---|---|
| Account topology | One configured broker context per process. Paper and Shadow have separate writable runner roots, custody and endpoints. The Shadow-side evaluator reads the Paper context through explicitly configured read-only sources. |
| Twin access | Add `--twin-live-state-root` to the existing CLI. Like `--live-state-root`, it means the parent artifacts directory containing `live_state/`. HTTP uses server-configured roots; the browser supplies account and instance identities, never paths or arbitrary URLs. |
| Progress authority | One Python rehearsal operation supplies the per-instance session evaluation to CLI and HTTP. The UI renders its states, counts, timestamps and explanations. No separate frontend graduation state machine is persisted. |
| Receipt creation | Reuse the existing receipt format and writer. An explicit authenticated action reevaluates the selected pair and seals only a satisfied result. Polling and status reads remain pure. |
| Live preparation | A read-only server response supplies the existing deployment form with the Shadow configuration and source receipt. The ordinary deploy path creates a new instance. Configuration edits are allowed, but a mismatched successor cannot claim the old rehearsal at arming. |
| Durable predecessor link | The arming plan and resulting arming record own the link: Shadow instance ID, Shadow whole-seal hash, and the existing receipt hash. The record already identifies the Live successor and its whole seal. No separate graduation-permission ledger or mutable rewrite of an old seal is introduced. |
| Arming identity | Resolve the requested instance first, then verify the authority appropriate to its sealed account. A real Live instance uses verified real Live activation, not a census of Shadow activation records. Preserve legitimate Shadow-bound rehearsal ceremony behavior without counting those instances as armed on Live. |
| Optional versus invalid rehearsal | No source selected means the existing direct-Live route, with no rehearsal claim. An explicitly selected source that is missing, corrupt, mismatched or insufficient refuses; it never silently becomes a null receipt. |
| Browser ceremony | The browser is another authenticated adapter over the same plan/apply/disarm operations as the CLI. Plan is read-only; apply checks the reviewed content, bounded expiry and current inputs. Every new mutating route requires authenticated control even if a development configuration permits other unauthenticated operations. |
| Activation | Shadow activation and real Live cutover remain supervised offline commands. The UI shows readiness, instructions and the subsequently observed authority, and never exposes shell execution or an activation-record editor. |
| Repeated apply | One plan can create at most one arming record. A retry returns the original outcome plus current status, or a typed already-applied result. It cannot renew an expired/revoked grant. A deliberate renewal requires a new plan. |
| Storage compatibility | Preserve and verify existing version-1 arming records using their original payload/hash rules. New plans and arming records use a versioned shape for predecessor fields and the originating plan ID. Existing receipts retain their format. No historical record is rewritten or silently “upgraded.” |

The browser actions supersede the CLI-only surface choices in the slice-4 and slice-6 designs. Prepare an explicit ADR amendment for review with those changes, preserving the offline cutover decision and all admission invariants. This plan is sufficient to build a concrete reviewable implementation in isolation; do not mark that policy amendment Accepted solely because a plan was requested. Do not pause task 1 merely to seek approval for a future live operation.

## Task 0 — Establish the baseline and record the contract

**Read:** the audit's F1, F8 and F9; [ADR 0059](../../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md); [Shadow reference](../../references/alpaca-shadow-authority.md); [Live authority reference](../../references/alpaca-live-authority.md); [arming reference](../../references/alpaca-live-arming.md).

- [ ] Verify the current entry points and the focused test groups below. Record any divergence from the audited checkout rather than assuming these defects remain.
- [ ] Prepare the proposed ADR amendment described above. Correct contradictory current mandatory-Shadow/unimplemented-Live prose when updating the affected documents; retain dated historical rationale.
- [ ] Record deployment inputs still needed for a real run: Paper account, live account, eligible strategy, twin instance IDs, separate service endpoints, Clerk roots, runner roots and configured session/envelope values. A missing value stays unknown. Credentials remain server-side.
- [ ] Produce a read-only starting-state checklist: already Live, Shadow active, activation required, history proof refused, runtime unavailable. Distinguish broker observations from configuration and local durable evidence.

**Done:** the implementation contract and baseline are recorded, and no real environment was changed to make a test pass. Runtime unavailability is explicitly recorded, not a reason to stop task 1.

## Task 1 — Read the Paper twin from its own runner root (F9)

**Start in:** `PythonDataService/scripts/manage_alpaca_shadow.py`, `app/services/alpaca_shadow_reconciliation.py`, `app/services/bot_binding_repository.py`, and `tests/scripts/test_manage_alpaca_shadow.py`.

- [ ] Add a regression with genuinely separate Shadow and Paper runner roots and separate custody databases. Both valid bindings must be written through the repository's existing fixture/writer seam. Confirm today's CLI cannot find the twin in that arrangement.
- [ ] Add `--twin-live-state-root`. Its omission keeps the existing common-root behavior; when specified, read the twin only from that root. Keep `--twin-artifacts-root` as the Paper Clerk root.
- [ ] Extract the current evaluation/summary/receipt orchestration out of the CLI into one reusable Python operation. Prefer extending the reconciliation service where cohesive; a small sibling module is justified if it keeps formatting/I/O orchestration separate from comparison math. The CLI becomes an adapter, not an import dependency of HTTP.
- [ ] Add optional server configuration for the same pair of Paper evidence roots, using the existing settings conventions. Both must be supplied together. Missing twin access blocks rehearsal evaluation, not otherwise-valid Live operation. Defaults never point at a developer's Paper artifacts accidentally.
- [ ] Use the existing confined path and read-only economic projection readers. The evaluator must not acquire a custody writer lease, initialize an authority, connect a Paper trade client, or share a writable runner root.

**Tests:** separate-root success; omitted-option compatibility; explicit missing twin; wrong Paper account; Shadow or synthetic identity masquerading as the named Paper account; mismatched parameters/action plan/size/session policy; invalid path and unreadable projection. Assert evaluation leaves evidence and control state unchanged.

**Done:** the actual CLI evaluates the named pair across independent roots using canonical readers. Its session report and receipt behavior remain compatible. Both read-only source locations can be configured for a future HTTP caller.

## Task 2 — Show per-instance Shadow progress in the Alpaca UI

**Start in:** `app/services/alpaca_shadow_reconciliation.py`, `app/services/alpaca_live_verdict.py`, `app/schemas/alpaca_live_verdict.py`, `app/routers/broker_v2_panel.py`, `Frontend/src/app/components/broker/v2-panel/`, and the existing Alpaca desk.

- [ ] Add a typed, account-and-instance-scoped read response using the task-1 operation. Include the selected twin identity, observation time, evidence availability, required/passed sessions, each day's state and authored explanation, fill diagnostics, receipt if present, and available next actions with reasons.
- [ ] Keep startup/account readiness readable when no primary authority is installed. Compose available facts instead of calling the deployment projection that currently returns 503 before offering any modes. Unknown broker identity must remain unknown, never inferred from an environment name.
- [ ] Expose a read route under the canonical `/api/brokers/alpaca/...` family, account-scoped consistently with the existing panel. Keep schemas outside the router. Reuse existing broker/account resolution and typed error translation.
- [ ] Add a compact “Shadow rehearsal” section to the selected V2 instance panel, linked from the Alpaca desk. Show the chosen Paper twin and its instance. Extend the existing broker client; reuse polling infrastructure and discard late responses after an account/instance selection change.
- [ ] Render counts and verdicts from Python. Session instants remain epoch milliseconds UTC, formatted locally with an explicitly labelled Eastern close where relevant. Use `receiptLabel` for code-like evidence and `app-asset-identity` for displayed instruments.
- [ ] Keep the global account banner an overview. A receipt for instance A must never make instance B's progress look complete.

**Tests:** each canonical non-counting state, satisfied/unsatisfied reports, zero-fill qualifying day, price/time drift diagnostics, a completed sibling, uninstalled authority, unavailable twin source, stale request arriving after instance switch, and no mutation from repeated polling. Assert visible copy explains the current 20:00 Eastern close rather than displaying an RTH close as sufficient.

**Done:** an operator can identify the exact incomplete session and reason from the UI. The UI does not calculate qualification or present an account-wide receipt as per-instance proof.

## Task 3 — Seal and inspect the selected rehearsal receipt

**Start in:** task-1 orchestration, `app/broker/alpaca/clerk/shadow_receipt.py`, `scripts/manage_alpaca_shadow.py`, and the task-2 panel section.

- [ ] Add an explicit authenticated receipt action for the selected account, Shadow instance and Paper twin. Resolve roots on the server and reevaluate immediately before sealing; a previously green page is not authorization to write a stale success.
- [ ] Call the existing receipt creator/store. Keep the CLI and HTTP on the same operation, count policy, time source and refusal semantics. Browser callers cannot provide `now_ms` or lower the configured required session count.
- [ ] Show the durable receipt hash, named sessions and predecessor configuration. Present timing/price drift as diagnostics and explain that the receipt proves the recorded rehearsal criteria, not execution quality.
- [ ] On a repeated unchanged request, return the existing equivalent qualifying receipt instead of manufacturing additional success history. If evidence changed, reevaluate and report the new outcome. Keep this deduplication inside the existing write discipline.
- [ ] Display a supplied offline activation procedure when Shadow is unactivated; offer refresh to observe the result. Do not add a live-server activation command to this action.

**Tests:** satisfied write through the real temporary ledger; unsatisfied/no-write; green read followed by changed twin/no-write; wrong account; configured count cannot be bypassed; unauthenticated/no-write; repeated request; corrupt receipt; CLI and HTTP equivalent domain outcomes.

**Done:** the operator can create and inspect the exact instance's qualifying receipt in the UI. No UI interaction can make an unsatisfied report qualify.

## Task 4A — Resolve arming identity from the exact instance (F1)

**Start in:** `app/broker/alpaca/clerk/live_arming_ceremony.py`, `live_authority.py`, `sqlite/activation.py`, `scripts/manage_alpaca_arming.py`, `tests/broker/alpaca/clerk/test_live_arming_ceremony.py`, and `tests/broker/alpaca/clerk/sqlite/test_cutover_live.py`.

- [ ] Promote the audit's never-rehearsed Live cutover → sealed Live instance → arming reproduction into a regression. Its fake account must actually pass the temporary initialize/plan/apply cutover, not merely use an invented activation mock.
- [ ] Read the requested binding and verified seal first. For a real Live binding, verify that exact account's activation against the canonical activation evidence and database identity/generation. Reuse/extract the existing verification operation; do not open a second writer lease or use only `ActivationStore.latest()` as proof of validity.
- [ ] Retain the appropriate verified Shadow activation path for Shadow-bound ceremony callers. Extra unrelated account records must not override an exact valid binding. Ambiguous or inconsistent evidence for the requested identity refuses.
- [ ] Keep unsealed/wrong-account bindings refused and the existing emergency disarm behavior available when activation evidence is missing or corrupt.
- [ ] Fix the focused incomplete-environment test to suppress dotenv explicitly at its settings seam. Avoid altering real `.env` or globally changing application settings behavior.

**Tests:** valid real Live without Shadow succeeds; wrong account, corrupt activation, wrong database generation and unsealed binding refuse; Shadow-bound behavior remains scoped; multiple unrelated activation rows do not choose the account; disarm remains possible after activation-evidence loss.

**Done:** real Live arming no longer discovers identity exclusively from Shadow history. The optional-rehearsal contract works without removing verified authority checks.

## Task 4B — Bind the Live arming record to its Shadow predecessor (F8)

**Start in:** `live_arming_ceremony.py`, `live_arming.py`, `live_arming_ledger.py`, `shadow_receipt.py`, `scripts/manage_alpaca_arming.py`, and their existing tests.

- [ ] Add the audited regression: distinct Shadow and Live instance IDs, same configuration, valid predecessor receipt. Today's Live plan loses the receipt. Use real temporary binding and receipt writers.
- [ ] Add an optional explicit rehearsal-source argument to arming plan. Resolve it from retained Shadow bindings, never by taking the first account receipt or guessing from a signal hash. Apply obtains the source from the reviewed plan, not a separate replacement argument.
- [ ] Verify source `shadow:<live account>`, target exact live account, distinct instance IDs, valid whole seals, current configured signal, action plan, size, carryover and session shape. Reuse the existing configuration-comparison logic; extract its world-independent portion if required without weakening `twins_agree`'s Paper gate.
- [ ] Verify the source receipt's hash, source identity, live account, configured signal and configured session requirement. Since the current receipt does not seal action plan/size directly, the immutable source binding and its whole-seal hash are essential inputs.
- [ ] Include source instance ID, source whole-seal hash and receipt hash in the versioned plan. Reobserve them at apply and refuse drift just as for the target seal and envelope. An explicit invalid source must fail; only an omitted source uses the direct-Live path.
- [ ] Append a versioned arming record containing the source fields, existing target fields and originating plan ID. Preserve version-1 readers and hashes; old records have no predecessor attribution. Unknown versions refuse. Keep the runtime arming/envelope gate's existing meaning.
- [ ] Make apply idempotent by plan ID within the ledger's existing account-scoped lock. A duplicate concurrent apply creates one grant. A retry after disarm or lapse returns the prior operation outcome with current status and never restores permission. Renew through a new plan.
- [ ] Expose the resulting predecessor reference in status so a refreshed UI can reconstruct the successful handoff without browser-local state or a second permission database.

**Tests:** matching predecessor succeeds; changed quantity/action plan/session/signal/account refuses even where signal hash alone matches; missing or corrupt explicitly selected source refuses; no-source direct Live succeeds; source/receipt/target/envelope drift after plan refuses; expired/forged plan refuses; version-1 ledger remains readable; new-record tampering refuses; duplicate and concurrent apply append once; replay after disarm cannot rearm; new-plan renewal works.

**Done:** the Live arming record proves which exact rehearsal was reviewed for which exact Live seal, and retries cannot mint additional grants. No old instance, receipt or custody record was rewritten.

## Task 5 — Guide offline graduation and prepare the new Live instance

**Start in:** `app/services/broker_v2_panel/panel_deploy.py`, `paper_deploy_service.py`, `app/schemas/broker_bots.py`, the existing deploy workflow, and [the cutover runbook](../../runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md).

- [ ] Add a read-only Live preparation operation for a selected receipted Shadow instance. Return the reviewed source facts and a draft for the existing deployment form. Server-side configuration resolution stays canonical; Angular does not recreate sizing, eligibility or seal rules.
- [ ] Present the offline transition as a distinct step: stop Shadow cleanly, preserve terminal outcomes, stop the writer, freeze runner-state writers, observe the real account flat/order-free, initialize if needed, review plan, apply before expiry, and restart under the observed Live authority. A flat synthetic book cannot satisfy the real-account check.
- [ ] Show exact observed identity and typed missing evidence. A current status response is not a replacement for the cutover CLI's fresh evidence and rechecks. Commands may reference server-configured paths but never include credentials; do not fabricate evidence JSON or confirmation tokens.
- [ ] Keep the actual cutover CLI and writer-lease rules unchanged. The UI does not offer reverse graduation or a fallback that deletes activation records. After refresh, it advances only on a verified real Live authority.
- [ ] Seed the existing deployment form with the source strategy, symbol, supported parameters, size and session configuration. Create a fresh Live instance through the ordinary deployment path. A source configuration the form cannot represent must produce an explicit preparation refusal, not altered defaults.
- [ ] Carry the selected source into the arming review. If the operator edits configuration, the server compares it again in task 4B. On refresh before arming, offer valid source candidates for explicit selection; the durable lineage is recorded only when arming succeeds.
- [ ] Preserve all strategy qualification, account access and current parameter-coverage checks. An operational harness is not promoted to Live merely because it generated a Shadow receipt.

**Tests:** blocked/unactivated/already-live states; flat Shadow with nonflat real account cannot appear cutover-ready; source draft matches actual sealed parameters; unsupported session shape is explicit; new Live ID and seal; existing Shadow binding unchanged; changed draft fails selected-source arming; current qualification still refuses ineligible strategies; restart observes the new authority rather than optimistically advancing.

**Done:** the operator can follow graduation without ambiguous mode labels, then create a new Live instance from the reviewed source. The offline operation remains visibly offline and supervised.

## Task 6 — Add supervised browser arming and existing loss-hold recovery

**Start in:** `live_arming_ceremony.py`, `app/routers/brokers.py`, `app/security/data_plane_control.py`, `Frontend/src/app/services/brokers.service.ts`, `alpaca-live-verdict.service.ts`, the V2 panel, and `Frontend/src/app/shell/alpaca-live-banner.component.ts`.

- [ ] Add typed status/plan/apply/disarm HTTP adapters over the task-4 operations. Use the established broker route family and account/instance scope. Follow the relevant stack skill for router placement; extract domain-specific transport if the existing router would become a catch-all.
- [ ] Require authenticated control on each mutation and validate the path account/instance against the actual plan/binding. The browser cannot provide filesystem roots, credentials, an alternate clock, force flags, or envelope overrides. Use the existing same-origin proxy/authentication chain; do not expose the data-plane secret to JavaScript.
- [ ] Review screen: exact Live account, instance, source rehearsal, configuration, configured limits, session lifetime, expiry and consequence of applying. The server-generated plan is immutable in the form. Applying never creates a replacement plan automatically.
- [ ] Require an explicit operator apply action. Disable duplicate clicks while in flight, rely on task-4B idempotency for uncertain network outcomes, and reread status after success or ambiguity. Changes to account/instance/source discard the current review. Expired or drifted plans explain why a new review is needed.
- [ ] Distinguish deployed, running, access enabled, armed, lapsed and disarmed. Explain that arming an already running instance can allow its next eligible real ENTER without another Start. Disarm prevents new entries and does not mean flatten; EXIT management remains available under current rules.
- [ ] Wire the existing guarded loss-hold-clear operation into the account UI. Reuse its backend refusal/re-observation. Preserve active-loss and unknown-evidence refusal; show the returned result rather than clearing the banner optimistically. Confirm this mutation cannot use an unauthenticated development bypass for a live authority.

**Tests:** unauthenticated mutations refused; account/instance mismatch; expired/drifted/tampered plan; no mutation from GET or plan; duplicate apply/uncertain response; account switch during review; disarm and renewal; readable unarmed versus unreadable arming evidence; loss hold still active; loss evidence unknown; EXIT continues when entries are blocked. Exercise the proxy control guard tests if its integration changes.

**Done:** CLI and UI use the same domain ceremony. The operator can deliberately grant/revoke entry permission and request guarded hold release without editing a ledger or running an undocumented command.

## Task 7 — Prove the integrated journey and finish the documentation

- [ ] Add one Python integration scenario using separate temporary Paper and Shadow stores and fake broker ports: matching runs → counted sessions → receipt → stopped-writer Live cutover → new Live instance → linked arming → fake real trade-port submission → broker fill evidence → reconciliation → EXIT.
- [ ] Include wrong-source, changed-plan and held/unarmed branches. Spy on effect ports: Shadow never invokes the real writer; only an armed eligible Live ENTER does. Include an active-run scenario proving apply may enable the next decision without another Start.
- [ ] Add one Playwright journey under `Frontend/tests/e2e/` using deterministic mocked API responses, following the existing test harness. Exercise an incomplete rehearsal, receipt success, offline cutover instructions, observed Live activation, prepared deployment, plan drift, fresh review/apply and persisted status. Stub the offline operation's resulting observation; the browser test must not execute shell commands or hit a broker.
- [ ] Run an accessibility check for the new controls and keyboard review flow. Verify stale/read-error states visibly preserve uncertainty.
- [ ] Regenerate OpenAPI and frontend generated types in the same change as their schemas. Keep manual button references generated from canonical operator copy where the repository requires it. Do not hand-edit generated inventories to make checks pass.
- [ ] Update the operator manual and current Shadow/Live/arming references with the final behavior. Add the accepted amendment only through the project's actual decision process. Resolve F1/F8/F9 and F5 in `known-gaps.md` only after their corresponding acceptance evidence passes; retain remaining limitations.
- [ ] Deliver a software-verification record with exact commands/results and explicitly separate any real-runtime checks that could not be performed.

**Done:** the route is proven across actual local domain operations and independently through the browser's user interactions. Neither proof is labelled a completed real Shadow rehearsal or a live trading validation.

## Targeted validation map

Use the current manifests and existing test runner setup. These are verified starting targets, not a request to run the entire suite after every edit. Add tests for new interfaces and expand to consumers of shared helpers when those helpers change.

| Changed behavior | Existing test targets under `PythonDataService/tests/` |
|---|---|
| Twin roots, evaluation and receipts | `scripts/test_manage_alpaca_shadow.py`, `services/test_alpaca_shadow_reconciliation.py`, `broker/alpaca/clerk/test_shadow_receipt.py`, `broker/alpaca/clerk/test_shadow_sessions.py` |
| Isolation and live risk rehearsal | `broker/alpaca/clerk/test_shadow_broker.py`, `broker/alpaca/clerk/test_shadow_envelope_runtime.py`, `broker/alpaca/clerk/sqlite/test_runtime_shadow.py` |
| Arming identity, lineage, retry and compatibility | `scripts/test_manage_alpaca_arming.py`, `broker/alpaca/clerk/test_live_arming_ceremony.py`, `broker/alpaca/clerk/test_live_arming.py`, `broker/alpaca/clerk/test_live_arming_ledger.py` |
| Live gate and retained EXIT behavior | `broker/alpaca/clerk/test_live_arming_gate.py`, `broker/alpaca/clerk/sqlite/test_arming_admission.py`, `broker/alpaca/clerk/sqlite/test_live_envelope_sync_arming.py`, `services/test_live_arming_admission.py`, `services/test_run_admission.py` |
| Cutover and authority | `broker/alpaca/clerk/sqlite/test_cutover_live.py`, `broker/alpaca/clerk/test_live_authority_runtime.py` |
| Deployment and visible state | `broker/v2panel/test_panel_deploy_shadow.py`, `broker/v2panel/test_panel_deploy_live.py`, `broker/v2panel/test_shadow_operator_surfaces.py`, `routers/test_brokers_live_envelope.py` |

Frontend consumer tests include the touched V2 panel/desk specs, `alpaca-deploy-workflow.component.spec.ts`, `alpaca-deploy-drawer.component.spec.ts`, `deploy-paper-access.component.spec.ts`, `deploy-launch-receipt.component.spec.ts`, `alpaca-live-banner.component.spec.ts` and `alpaca-live-verdict.service.spec.ts`. Run exact spec paths using the installed Angular runner; the existing Playwright configuration expects a separately served frontend and uses `PLAYWRIGHT_BASE_URL` when supplied.

Before completing a Python change, run project-scope Ruff per `.claude/rules/python.md`. Before completing schema/UI changes, regenerate with `PythonDataService/scripts/export_openapi_contract.py` and the frontend `codegen:openapi` script, then run their check commands, frontend guard checks, targeted tests and build. Run `scripts/check_documentation_contract.py` and `git diff --check` for documentation/patch hygiene. Record pre-existing failures with baseline evidence; never “fix” a negative test by allowing the invalid Live operation it is meant to reject.

## Subsequent supervised operating sequence

This is the real-world acceptance phase after the implementation is reviewed and deployment is authorized. Terra prepares the concrete evidence and commands first; actual account-changing actions require explicit authorization for the selected account and instance. This plan itself supplies none.

| Stage | Evidence to collect before advancing |
|---|---|
| Inventory | Broker-confirmed account IDs, installed worlds, credential-mode agreement, activation/history eligibility, qualified strategy and actual configured envelope. If services are unavailable, report that condition. |
| Two-context setup | Independently configured Paper and Shadow processes, isolated writable state, reachable read-only twin evidence, working authentication and market data. |
| Shadow activation and deploy | Reviewed activation command; observed Shadow authority; correctly matched Paper/Shadow instances and admitted runs. |
| Rehearsal | Both runs cover each decision session, clean sweep through the actual declared close, configured number of qualifying sessions, reviewed price/time diagnostics and representative order behavior. |
| Receipt | Exact instance receipt and source whole seal. No sibling's banner or zero-exit status substitute. |
| Live cutover | Stopped writers and durable terminal outcomes; fresh evidence that the real account is flat and order-free; reviewed plan/apply; observed real Live authority after restart. |
| New instance and arming | New Live ID/seal; current qualification; explicitly reviewed source link and envelope; applied arming outcome and current status for that instance. |
| First Live lifecycle | Operator-selected size and strategy; actual acknowledgement, fills, custody reconciliation and exit evidence. Arming status alone does not satisfy this stage. |

Use the established Linux storage domain: default Compose Clerk root `/app/artifacts/alpaca_clerk` on its named volume, and parent runner root `/app/artifacts`. The operator `--live-state-root` options also take that parent, not `/app/artifacts/live_state`. Run authority commands inside the supported Linux environment and follow the cutover runbook's stop boundary. A macOS copy of a live database/WAL/SHM is not an evidence transport. Preserve the current activation and binding records through the transition; proposed future cleanup never substitutes for a valid graduation.

## Terra's final handoff

Report completed task numbers, changed interfaces, regression evidence, exact validation results, decision-amendment status and residuals. Include the operator's next concrete step with verified IDs/paths only when actually observed. State separately whether software is verified, real rehearsal is complete and Live execution is verified. If only the software phase was authorized, deliver it without activating or arming a real account.
