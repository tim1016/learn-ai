# Alpaca Paper / Live workflow and authority audit

**Observed:** 2026-09-09 America/Chicago (2026-09-10 UTC).
**Checkout:** `97992392`, including live slice 7 / PR #2015.
**Standing:** Supporting investigation. Findings describe current code and tests; proposed changes below are not accepted architectural decisions.

The core architecture already shares strategy execution and SQLite Clerk custody across the account worlds. The missing layer is a complete operator workflow: configuration selects one account for the process, boot selects its custody world, and important live operations remain outside the UI. One reproduced defect also leaves Shadow activation as a hidden prerequisite for arming a newly activated Live account.

The right direction is to retain the shared execution machinery and separate three questions in the product: **which account am I viewing, what kind of execution will this new instance perform, and is this exact instance permitted to submit?** A view selector should not change an existing bot's account or its permission to trade.

**Current user direction:** Keep Shadow and establish a concrete Shadow → Live journey. The [selected route](#selected-route-shadow-rehearsal-to-live) below takes priority over the broader implementation roadmap. Choosing rehearsal for this journey does not make it mandatory for every future Live deployment or change ADR 0059's optional-rehearsal policy.

The subsequent [Terra implementation handoff](../superpowers/plans/2026-09-10-shadow-to-live-operator-journey.md) turns that route into bounded implementation tasks, regression cases and completion criteria. It is a proposed plan, not evidence that the changes or real-account operations have occurred.

## Evidence and limits

- Read the current configuration, broker registry, authority selection, binding selection, deployment projection, admission policy, arming ceremony, loss-hold recovery, Angular deployment controls, and relevant ADRs.
- Inspected environment variable names, presence, and selected non-secret flags only. Both checked environment files select `ALPACA_MODE=live`. Each has one active pair under the names the application consumes. This does not establish whether additional credentials are kept in comments or elsewhere, or which mode another running installation uses.
- The Compose data service consumes `PythonDataService/.env`; root `.env` feeds Compose interpolation. `AlpacaSettings` also reads a relative `.env` when used directly, making the working directory relevant to host commands.
- Local UI inspection at `http://localhost:4200` failed with connection refused. No listeners were found on the checked development ports. This is a code and isolated-test audit, not a reproduction in the user's running brokerage session.
- A subsequent container inventory also failed because the local Podman connection was refused. Current account identity, installed activation, running instances, existing receipts, and qualifying session count remain unverified. No service was started to obtain them.
- No real broker orders, account mutations, arming records, credential changes, or service restarts were performed. The arming reproduction wrote only to an automatically removed temporary directory.
- Existing focused checks: **201 Python tests and 47 Angular tests passed**, with the Python ceremony group isolated from the developer's `.env` as described below. A separate reproduction intentionally fails on the newly identified arming defect.

## What the four worlds actually mean

| User label | Account context | Who creates fills? | Order contact with Alpaca | Custody identity |
|---|---|---|---|---|
| Dry Run | Local synthetic account; current market-data feed | The app's synthetic execution implementation | None from the execution path | `sim:<instance>` |
| Paper | Alpaca paper account | Alpaca's paper environment | Paper endpoint | Exact paper account ID |
| Shadow | Observations of a live account, with isolated simulated custody | The app's no-submit execution implementation | Live reads; no real order submission | `shadow:<live_account_id>` |
| Live | Alpaca real-money account | Broker execution | Live endpoint, subject to instance arming and account gates | Exact live account ID |

Paper is also a simulation, but it is the broker's simulation. Alpaca documents separate paper credentials/endpoints with the same API specification, and simulated fills rather than exchange routing. This supports one adapter architecture with distinct account configuration. It does not imply that paper execution quality equals live execution quality. [Alpaca Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)

Shadow is deliberately more subtle than a read-only account page: `ShadowAccountReadPort` delegates account/clock/asset/activity observations to the live read port, while its order and position reads come from its synthetic order book. The live envelope has a separate observation of the real account. These facts must remain visibly distinct in projections.

The implementation shares the important seams:

- A broker-neutral Signal Program produces decisions and semantic action requests; it does not choose credentials or submit orders. See [ADR 0042](../architecture/adrs/0042-sealed-signal-and-account-scoped-custody-authorities.md).
- [BindingAuthoritySelector](../../PythonDataService/app/services/bot_binding_authority.py) selects custody/evidence by the immutable binding. Dry Run has an isolated authority; broker execution uses the current primary authority.
- [Active authority selection](../../PythonDataService/app/broker/alpaca/clerk/active_authority.py) and [shared runtime composition](../../PythonDataService/app/broker/alpaca/clerk/active_runtime.py) reuse the SQLite Clerk implementation. Paper and Live are not independent strategy engines.
- [Account authority contracts](../../PythonDataService/app/schemas/account_authority.py) carry account ID and authority kind together and reject mixed-world aggregates.
- [Run admission](../../PythonDataService/app/services/run_admission.py) and the Clerk's [ENTER path](../../PythonDataService/app/broker/alpaca/clerk/sqlite/enter.py) answer different questions: whether a run may operate, and whether a particular new exposure may be accepted.

The audit did not establish a second competing Alpaca custody engine that should be deleted. One implementation with one writer per account authority is compatible with multiple isolated account contexts.

## Findings

### F1 — An activated Live account still needs Shadow activation to plan arming

**Confirmed functional defect; high priority.**

The September 9 amendment to ADR 0059 makes rehearsal optional. The live cutover test proves a never-rehearsed live account can be initialized and activated. But `live_arming_ceremony.live_account_id_for()` discovers the account exclusively through `ShadowActivationStore.account_ids()`. With no Shadow activation, it refuses before reading the valid live-bound instance, returning `LIVE_ARMING_INSTANCE_UNSEALED` and directing the operator to activate Shadow.

An isolated reproduction performed a real temporary initialize → plan → apply cutover, created a live-sealed binding using the repository's fixture writer, and invoked the actual arming CLI's read-only `plan` operation:

| Otherwise identical inputs | Arming plan result |
|---|---|
| Valid live activation and live-sealed instance; no Shadow activation | Exit 2, `LIVE_ARMING_INSTANCE_UNSEALED` |
| Add only a Shadow activation fence; no rehearsal and no Shadow receipt | Exit 0; `shadow_receipt_sha256=null` |

This is specifically a dependency on the Shadow **activation record**, not a remaining requirement to complete rehearsal sessions. The old test `test_no_shadow_activation_proof_refuses_before_any_binding_is_read` still pins the dependency, while newer tests prove the receipt is optional. These tests miss the complete never-rehearsed Live → arming path.

**Fix direction:** Resolve arming identity from the exact sealed instance and verified account authority appropriate to its world. Preserve account agreement, activation verification, ambiguity refusal, seal binding, plan freshness, and re-observation on apply. Do not solve this by accepting an arbitrary typed account ID or by removing identity checks.

Evidence: [arming ceremony](../../PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py), [arming CLI](../../PythonDataService/scripts/manage_alpaca_arming.py), [live cutover tests](../../PythonDataService/tests/broker/alpaca/clerk/sqlite/test_cutover_live.py), [ceremony tests](../../PythonDataService/tests/broker/alpaca/clerk/test_live_arming_ceremony.py).

### F2 — The configuration supports one selected credential pair, not two account profiles

**Confirmed capability gap relative to the requested toggle.**

`AlpacaSettings` consumes `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, and `ALPACA_MODE`. It caches a process-wide settings object; the SDK client is also cached. The endpoint is correctly derived from the mode. There is no built-in paper-pair/live-pair selection table.

The registry maps `broker_id` to one port, and the default registration installs one `alpaca` entry. Account-scoped routes validate their account ID against that one broker account. Merely keeping two pairs in an environment file therefore does not make both accounts selectable.

A global mode toggle would need to coordinate clients, streams, cached account observations, writer leases, recovery, and already-bound runs. A browser account selector should instead select an explicitly configured account context. Existing instances must retain their immutable account binding when the operator switches views.

Evidence: [settings](../../PythonDataService/app/broker/alpaca/config.py), [client](../../PythonDataService/app/broker/alpaca/client.py), [registry](../../PythonDataService/app/broker/contract/registry.py), [account scope](../../PythonDataService/app/services/broker_v2_panel/panel_scope.py), [Compose](../../compose.yaml).

### F3 — Shadow and Live are mutually exclusive primary worlds at boot

**Confirmed, deliberately deferred capability; not an accidental missing radio button.**

The observed deployment choices are:

| Selected primary authority | Available choices |
|---|---|
| Paper | Dry Run, Paper |
| Shadow | Dry Run, Shadow |
| Live | Dry Run, Live |

For a live account, an absent live activation takes the Shadow selection path, which requires its own activation. A present live activation takes the real-money selection path. Invalid activation does not silently become another usable authority.

After live activation, the same process cannot offer Shadow for a new strategy. Slice 7 explicitly deferred coexistence. This explains why the user cannot treat Shadow as an always-available rehearsal choice after graduating the account.

**Fix direction:** Extend exact-binding authority selection to an isolated Shadow authority beside the real Live authority, using the existing synthetic secondary-authority pattern. Keep account IDs, custody, evidence, and effect ports separate. Do not retarget existing bindings when a mode control changes.

Evidence: [deployment mode projection](../../PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py), [authority selector](../../PythonDataService/app/broker/alpaca/clerk/active_authority.py), [documented coexistence deferral](../superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md).

### F4 — Deployment, access approval, and arming do not form one UI workflow

**Confirmed product gap, partly intentional policy.**

The UI can review and enable the exact program/account pairing, choose a permitted execution mode, and deploy. On Live, a readable but unarmed instance may start and manage exposure; the Clerk refuses its ENTERs until arming. Unreadable arming evidence blocks launch. Thus **running**, **access enabled**, and **armed** are three different states.

The live deployment receipt then instructs the user to run `scripts.manage_alpaca_arming plan` and `apply`. Arming status/plan/apply/disarm exist as CLI operations; there is no corresponding HTTP ceremony or UI. The shell shows a live verdict and armed count but supplies no arming controls.

The CLI-only boundary is explicit in the accepted design, so a browser ceremony needs an architectural amendment defining how authenticated, reviewed plan/apply remains supervised. This report proposes that work; it does not silently replace the rule.

The program/account grant is also still named `paper_access_state` internally. Its visible labels now adapt to Paper/Shadow/Live, while `UI_ACTIVATION_REASON` still records “Enable Paper access from the Alpaca Deploy page.” for all three. The grant is not per-instance real-money arming, regardless of the visible “Live access enabled” label.

Evidence: [deployment receipts](../../PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py), [arming admission contract](../../PythonDataService/app/schemas/run_admission.py), [access UI](../../Frontend/src/app/components/broker/broker-deploy-page/deploy-paper-access.component.ts), [live banner](../../Frontend/src/app/shell/alpaca-live-banner.component.ts), [ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md).

### F5 — Loss-hold recovery exists in the backend but has no UI action

**Confirmed recovery gap.**

`POST /api/brokers/{broker}/live-envelope/loss-hold/clear` is authenticated and calls the canonical guarded clear. It re-observes the account and refuses while the breach remains or the required facts cannot be proved. The frontend generated contract includes the endpoint, but the application has no service call or control for it. The banner only displays that a loss hold exists.

**Fix direction:** Expose that existing guarded operation with its current explanation and outcome. Keep it distinct from generic Clerk hold recovery and preserve the account-wide ENTER refusal while EXIT continues.

Evidence: [broker router](../../PythonDataService/app/routers/brokers.py), [guarded clear](../../PythonDataService/app/services/alpaca_live_envelope.py), [banner](../../Frontend/src/app/shell/alpaca-live-banner.component.ts), [frontend broker client](../../Frontend/src/app/services/brokers.service.ts).

### F6 — The current explanations disagree about what is built and required

**Confirmed documentation and operator-copy drift.**

- The canonical operator manual says Live is unreachable/stubbed and says the evidence-only override is unavailable for Paper. Current deployment code and tests support live execution and require the explicit override on an evidence-only broker deployment.
- ADR 0059's amendment makes Shadow optional, but its title, Decision 2 heading/opening, and some explanatory sentences still present it as required. `CONTEXT.md` still defines “Shadow gate” as a prerequisite before arming.
- On the Paper deployment view, the disabled Live card says live execution is not connected to an admission or execution path. That describes an older implementation, rather than why the currently selected Paper account cannot submit live orders.
- Some comments still describe Dry Run as having no Clerk custody, although its current execution uses an isolated synthetic SQLite Clerk.

These contradictions make it difficult to tell a deliberate safety refusal from unfinished functionality. Correct the current manual and operator copy, and reconcile amended ADR/glossary text while preserving the recorded decision history. A historical type name alone is not evidence of a second authority.

Evidence: [manual](../broker-v2-operator-manual.md), [glossary](../../CONTEXT.md), [ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md), [mode cards](../../PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py), [Dry Run execution](../../PythonDataService/app/services/bot_trade_strategy.py).

### F7 — Dry Run's UI entry point depends on the real-account deployment projection

**Confirmed coupling; the desired independent workflow needs a product decision.**

The Dry Run eligibility calculation intentionally ignores real-account freeze, holds, and outstanding intents. However, the request that supplies the entire deployment form first resolves the broker account, checks the installed primary world, and reads Clerk status.

An isolated call with a readable live account and no installed authority returns HTTP-equivalent 503 before constructing any mode choices: “No Alpaca authority is installed, so deployment assumes the paper world…” Dry Run is unreachable through this form in that state even though its execution authority is synthetic. This is a read-path coupling, not proof that a running Dry Run submits broker orders.

The catalog also filters to currently human-validated entries before producing mode eligibility. The manual's claim that any runtime-backed strategy can be selected for Dry Run therefore does not describe the complete UI path. Current sealed-program build/validation rules must be reconciled before changing that filter.

Evidence: [deployment orchestration](../../PythonDataService/app/services/broker_v2_panel/panel_deploy.py), [strategy catalog](../../PythonDataService/app/services/broker_v2_panel/strategy_catalog.py), [Dry Run eligibility tests](../../PythonDataService/tests/broker/v2panel/test_dry_run_eligibility.py).

### F8 — A new Live instance does not inherit its predecessor's Shadow receipt

**Confirmed evidence-continuity gap for the selected journey; not an arming refusal.**

Graduation requires a new instance because the custody account is inside the immutable seal. But `observe_arming_inputs` asks `ShadowReceiptStore.current` for the exact instance being armed. It has no predecessor argument. A receipt under the Shadow instance ID therefore does not accompany the new Live instance, even when their configured signal, action plan and size agree.

An isolated temporary-directory probe used the repository's fixture writers to create a Shadow binding, a matching new Live binding, a valid Shadow activation fence and a qualifying receipt for the Shadow binding. The actual arming input reader found the receipt for the Shadow instance and returned `shadow_receipt_sha256=null` for the Live successor. No broker client was called and no arming record was applied. This pins a missing link, not an invalid receipt or a failure of the optional-rehearsal policy.

**Fix direction:** Add an explicit, verified predecessor reference to the graduation workflow. Check the live account, full rehearsal configuration, session policy and receipt; preserve distinct instance IDs and seals. A matching signal hash alone is insufficient because it does not establish the action plan and size. Before that exists, retain a separately reviewed transition record naming both instances and the receipt, and describe the missing automatic link honestly. Never rewrite the old receipt's instance ID or reuse the Shadow seal on Live.

Evidence: [arming input reader](../../PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py), [receipt lookup](../../PythonDataService/app/broker/alpaca/clerk/shadow_receipt.py), [immutable Live deployment](../references/alpaca-live-authority.md), [fixture writers used in the probe](../../PythonDataService/tests/broker/alpaca/clerk/live_arming_fixtures.py).

### F9 — The twin operator reads both bindings from one runner root

**Confirmed tool limitation for separate Paper and Shadow installations.**

`manage_alpaca_shadow._judge` constructs one binding repository from `--live-state-root`, then reads both the Shadow and Paper instance from it. `--twin-artifacts-root` selects only the Paper custody database. There is no separate twin runner-root option. The documented Paper-host topology therefore needs additional evidence access when the installations keep their bindings in separate roots.

**Fix direction:** Support an explicit read-only Paper binding source alongside its custody projection, validate that both name the same twin, and exercise the operator against two independent roots. Keep runtime roots isolated; sharing writable runner state between two processes is not a substitute. This smaller prerequisite can be completed before building a general account-profile selector.

Evidence: [operator argument parser and judge](../../PythonDataService/scripts/manage_alpaca_shadow.py), [twin identity and run coverage](../../PythonDataService/app/services/alpaca_shadow_reconciliation.py), [documented operator recipe](../references/alpaca-shadow-authority.md).

## Selected route: Shadow rehearsal to Live

This is an operator plan derived from the current implementation. It has not been executed against an account. Shadow remains part of the product. The first graduation can be sequential; simultaneous Shadow and Live authorities are not a prerequisite for it.

### 1. Establish the starting state and the two account contexts

Read the actual broker identity, installed authority, activation records, instance roster and existing rehearsal evidence. A live-mode environment alone does not tell us whether the process will boot Shadow or Live. If a real Live activation already exists, the current process cannot return to Shadow; stop this route at that finding and implement the separate Shadow authority before rehearsing. Do not delete activation records to change modes.

For a qualifying rehearsal, run a Paper twin and Shadow instance during the same sessions. Today that means separately configured service contexts, since one process consumes one credential pair. Give each its own runner state, authority and process endpoints. Resolve F9 so the evaluator can read the Paper binding and economic evidence from the correct installation. Use supported read-only projections in the database's storage domain, not a copied live SQLite file.

**Pass condition:** exact Paper and live account identities are known; the live account is eligible for Shadow; both contexts can operate independently; the evaluator can identify both bindings and custody databases. Shadow startup currently refuses any historical Clerk-minted real order, and refuses when its bounded history query returns 500 rows without proving the namespace empty. Existing account history can therefore be a real blocker.

### 2. Activate Shadow and deploy a matched pair

Review the configured live risk envelope, rehearsal session count and arming lifetime. Activate the Shadow authority using `scripts.manage_alpaca_shadow activate`, then boot the live-configured service into Shadow. Its execution port creates simulated orders and has no live order writer.

In the Alpaca deployment UI, select an executable, validated strategy with the required build proof and parameter coverage. Enable the program/account pairing and create the Paper twin and Shadow instance. Record their distinct instance IDs. Match configured signal, symbol/data settings, session shape, action plan, size and carryover policy; do not substitute a Dry Run instance for the intended Paper-broker comparison.

**Pass condition:** the installed authority is reported as Shadow; both deployed instances have readable matching configuration and admitted runs. Neither account access approval nor Shadow activation is real-money arming.

### 3. Complete and inspect the rehearsal sessions

Start both runs before the strategy's decision session opens. Each side needs one run spanning that entire session. Keep the Shadow service sweeping cleanly through the recorder's declared close, currently **20:00 Eastern even for an RTH-only instance**. Stopping the service at 16:00 does not produce a completed day. A late start, missing run coverage, a non-clean sweep or a gating twin divergence prevents that day from counting.

Use `scripts.manage_alpaca_shadow sessions` for the exact Shadow instance and Paper twin. Read the returned `satisfied` value and each day's reason; exit code zero only means this reporting command answered. Accumulate the configured `ALPACA_LIVE_SHADOW_SESSIONS` count without lowering it simply to obtain a pass.

**Pass condition:** the per-instance report is satisfied, with complete coverage and no gating differences in fill count/symbol, direction or quantity. Review reported timing and price drift separately: the current comparison pairs fills by sequence, not by decision bar, and those drift diagnostics do not gate the receipt. Also review whether representative entry/exit behavior actually occurred; a quiet day can qualify without exercising an order lifecycle.

### 4. Seal the rehearsal result and prepare the Live handoff

Run the matching `receipt` operation only after the report is satisfied. Retain the receipt hash, both instance IDs, exact configurations, counted sessions and diagnostic review. The account banner's `complete` state can refer to another instance and is insufficient for this step.

Prepare the successor link described in F8. The current system can arm a new Live instance without that link, but cannot claim its arming record automatically includes the completed Shadow rehearsal. Prefer implementing the verified link before the first guided graduation; a reviewed external transition record is the available interim evidence.

**Pass condition:** the selected strategy has a valid receipt and an explicit account/configuration handoff record. Keep its activation and binding evidence intact during the transition; current arming identity still depends on the Shadow activation fence (F1).

### 5. Graduate the account through the offline Live cutover

Stop the Shadow instances cleanly, preserve their terminal outcomes, stop the account writer and freeze runner-state writers for the cutover ceremony. Obtain fresh observations proving the **real broker account** is flat and has no open orders. A flat Shadow book does not establish real-account flatness.

Follow the existing [SQLite cutover procedure](../runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md) with live account evidence: initialize the real account authority when needed, produce a cutover plan, review its account and evidence, then apply that exact plan with its confirmation token before expiry. The CLI consumes evidence; it does not fetch broker facts itself. Reobserve and replan if facts change.

**Pass condition:** a valid real Live activation names the exact broker account and custody database. After the controlled restart, the backend reports real-money authority installed. The old Shadow bindings remain historical and cannot start under that authority. This cutover currently has no supported reversal.

### 6. Create and arm the new Live instance

Deploy a **new** Live instance through the UI, preserving the reviewed strategy configuration and recording its predecessor. Review the exact Live account, whole instance seal, configured envelope and session expiry with `scripts.manage_alpaca_arming plan`. Applying the reviewed plan grants that specific instance permission for new real exposure.

**Pass condition:** `status` and the backend's instance verdict agree that this Live instance is armed with current evidence. The next eligible ENTER still needs clean account gates and the cash envelope. Arming may enable a run that is already running; there is not necessarily a second Start action before the first real order. Unarmed Live is also not a simulation: EXIT and recovery operations can act on real exposure.

### 7. Verify the first Live order lifecycle

For the operator-selected initial size and strategy, observe the next eligible real order through submission, broker acknowledgement, fills, custody reconciliation and eventual exit. Review the decision and order receipts against the account display. An armed badge alone is not evidence that this sequence works or that every next order will be admitted.

**Pass condition:** the observed broker lifecycle agrees with the Clerk's durable evidence. Confirm how the operator will disarm or renew entry permission and handle an account hold. Disarming prevents new entries; it is not a flatten command, and arming expiry deliberately leaves EXIT management available.

### Existing surfaces and work needed first

| Operation | Available today | Work for the guided journey |
|---|---|---|
| Strategy review, account access, Paper/Shadow/Live deploy | Alpaca UI, with choices depending on installed authority | Preserve matching predecessor configuration and show the exact next step |
| Two simultaneous Paper/Shadow contexts | Separate configured installations | Verify topology and add the separate twin binding source (F9) |
| Shadow activation, session report and receipt | Operator CLI | Present per-instance progress and qualifying reasons in the UI |
| Rehearsal-to-Live evidence handoff | Separate records; no automatic successor lookup | Verify and record the explicit predecessor link (F8) |
| Account graduation | Offline initialize/plan/apply ceremony | Guide the user to the existing stopped-writer procedure |
| Live arming, status and disarm | Operator CLI | A UI ceremony needs the documented policy amendment and the same reviewed plan/apply checks |
| Daily loss-hold release | Guarded authenticated API | Expose its current guarded result in the UI (F5) |

Immediate engineering order: **read-only readiness inventory → separate Paper twin evidence access → per-instance rehearsal progress/receipt → verified Live successor link → guided cutover and arming → first-order acceptance evidence**. Retain the existing Clerk, signal engine, risk envelope and refusal paths. The more general account-profile toggle and post-graduation Shadow coexistence remain useful later work; neither should obscure these concrete first-journey gaps.

For default Compose, the Clerk root is `/app/artifacts/alpaca_clerk` on the Linux-local named volume. Operator `--live-state-root` and cutover `--runner-artifacts-root` take the parent `/app/artifacts`, **not** `/app/artifacts/live_state`: the binding repository appends `live_state` itself. Resolve the actual deployment paths before executing commands, and follow the cutover runbook's storage/stop boundary. No production account IDs, tokens or evidence values are invented in this plan.

## A coherent user journey

The following is a proposal, not a change to accepted policy.

1. **Choose an account:** Paper or Live, from named profiles loaded server-side from the environment. Show the broker-confirmed identity, connection state, and available operations. Selecting a view changes neither an existing instance nor its arming.
2. **Choose a strategy and review evidence:** Show executable runtime, build qualification, validation, parameter coverage, and account permission as distinct facts. Keep one strategy definition and one evaluation contract.
3. **Choose execution:** Dry Run uses synthetic custody; Paper uses the paper broker; a Live account can offer Shadow rehearsal or real-money execution once their separate authorities are supported. Simulation source remains visible in every receipt.
4. **Create an immutable instance:** Bind account, execution world, parameters, size, and evidence choices. Moving a Paper strategy to Live creates a new instance with a link to its predecessor; it does not modify the old instance's seal or transfer its positions.
5. **Review and arm the Live instance:** Show the exact account, seal, configured envelope, session expiry, and whether a rehearsal receipt exists. Preserve read-only plan, explicit confirmation, input re-observation, drift refusal, and the sealed arming ledger.
6. **Operate and recover:** Distinguish running from entry permission. Show the backend's blocking reason with the actual available next action: renew arming, repair evidence, restore a connection, or request guarded loss-hold release. Preserve EXIT management when arming lapses or the loss hold rises.

Market-data connectivity is a separate operational dimension. A familiar visual control is reasonable, but turning a data feed on/off and choosing which account may accept orders have different effects. Those effects should be authored by the backend and stated at the control.

## Broader implementation roadmap and acceptance evidence

The selected Shadow → Live route above is the current next-work sequence. This broader roadmap retains the original account-selector and UI goals, including the never-rehearsed Live path that is not a prerequisite for the selected rehearsal.

| Slice | Concrete outcome | Acceptance evidence |
|---|---|---|
| 1. Repair the current Live path | Remove the hidden Shadow-activation dependency using verified exact-account evidence; correct contradictory current copy | A never-rehearsed activated Live account plans and applies arming for its own live-sealed instance; wrong-account, ambiguous, stale-plan, changed-seal, and changed-envelope cases refuse |
| 2. Complete operator controls | Guided live readiness/arming flow and guarded loss-hold recovery using existing domain operations | A user completes the reviewed ceremony and sees its durable result; stale inputs refuse; holds do not strand EXIT; required ADR amendment is captured |
| 3. Add configured account profiles | Paper and Live can both be viewed and selected without editing credentials or retargeting running instances | Two fake broker accounts with distinct clients, streams, caches, authorities, and receipts; switching views leaves existing run/account identity intact |
| 4. Add optional rehearsal beside Live | Shadow remains available after live activation; Dry Run has an explicit entry path | A Shadow instance and Live instance use the same signal contract but isolated custody; Shadow cannot reach the real write port; evidence cannot aggregate across worlds |

Before slice 3, settle whether the product permits concurrent Paper and Live operation or enforces one operational account at a time. Both need explicit profiles. If operation is exclusive, switching requires a defined drain/recovery procedure for the departing account; it cannot abandon open positions. The view selector can remain independent in either design.

## Cleanup that the evidence supports

- Remove the obsolete *mandatory* Shadow premise from current control flow and explanatory copy after F1 is repaired. Retain optional rehearsal, reconciliation, and receipt generation: they still have real callers and remain useful evidence.
- Replace misleading Paper-only operator prose in world-neutral flows. Rename `AlpacaPaper*`/`paper_access_*` contracts only as a coordinated generated-contract migration, not as a prerequisite for fixing user-visible behavior.
- `ALPACA_MARKET_ENDPOINT` is present in the inspected environment files but has no application/script consumer in the searched Python source. The SDK endpoint is derived from `ALPACA_MODE`; clarify that configuration rather than adding another endpoint authority.
- Retain synthetic and Shadow custody isolation, append-only instance/run evidence, and recovery readers unless a separate caller/artifact census proves a specific deletion safe. `DryRunActivityJournal` and Shadow receipt/reconciliation code have current callers; their names do not make them dead machinery.
- Keep retired IBKR control machinery outside the new design. This audit proposes no restoration or extension of those surfaces.

## Reproduction and test record

The new arming defect can be reproduced from `PythonDataService/` with the command below. It uses synthetic fixture credentials, a verified temporary live cutover, and the real read-only arming CLI path. It deliberately ends with an assertion failure while F1 remains open. The second probe adds only the Shadow activation record, isolating the dependency; neither probe applies arming or contacts a broker.

```sh
.venv/bin/python - <<'PY'
import contextlib
import io
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from scripts.manage_alpaca_arming import main
from tests.broker.alpaca.clerk.sqlite.test_cutover_live import (
    test_a_never_legacy_live_account_graduates_end_to_end,
)
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS, activate_shadow_fence, live_settings, record_sealed_binding,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT

with TemporaryDirectory(prefix="alpaca-mode-audit-") as tmp:
    root = Path(tmp)
    test_a_never_legacy_live_account_graduates_end_to_end(root)
    assert ActivationStore(root / "accounts" / "alpaca").latest(LIVE_ACCT)
    runner = root / "sealed-bindings"
    record_sealed_binding(runner, strategy_instance_id="audit-live",
                          sealed_account_id=LIVE_ACCT)
    args = ["--artifacts-root", str(root), "--live-state-root", str(runner),
            "plan", "--strategy-instance-id", "audit-live",
            "--now-ms", str(ARMED_AT_MS)]
    def plan_result():
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(args, settings=live_settings(_env_file=None))
        payload = json.loads(output.getvalue())
        return {"exit_code": code, "error": payload.get("error"),
                "shadow_receipt": payload.get("shadow_receipt_sha256")}
    without = plan_result()
    activate_shadow_fence(root)
    with_fence = plan_result()
    sys.stdout.write(json.dumps({"without_shadow_activation": without,
        "with_shadow_activation_only": with_fence}, sort_keys=True) + "\n")
    assert without["exit_code"] == 0, "Live arming still requires Shadow activation"
PY
```

Observed: without Shadow activation, exit 2 / `LIVE_ARMING_INSTANCE_UNSEALED`; with only its fence, exit 0 / null receipt. The enclosing command exits 1 at the assertion.

Existing tests executed:

- **56 passed:** `test_panel_deploy_live`, `test_panel_deploy_shadow`, `test_shadow_operator_surfaces`, `test_dry_run_eligibility`, `test_live_authority_runtime`, `test_live_arming_admission`.
- **145 passed with isolated working directory:** `test_manage_alpaca_arming`, `test_live_arming_ceremony`, `test_cutover_live`, `test_brokers_live_envelope`, `test_run_admission`.
- **47 passed across three Angular specifications:** deployment workflow, access approval, and live banner. Angular reported an unrelated `NG8102` template diagnostic in Strategy Lab run statistics; the specs passed.

The first run of the 145-test group from `PythonDataService/` had **144 passes and one failure**. Its incomplete-environment test deletes process environment variables but leaves Pydantic able to refill them from the developer's real `.env`. Running those same tests from a temporary directory with explicit `PYTHONPATH` and no local `.env` gives 145 passes. That is a test-isolation defect, not evidence that an actually incomplete live configuration is accepted. No test or application source was changed during this audit.

The passing component and module tests establish useful local contracts. They do not establish a complete browser-driven Paper → Live transition, and the F1 reproduction shows why that integration-level acceptance case is needed.
