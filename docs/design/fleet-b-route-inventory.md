# Fleet delivery B — complete scoped contracts: route inventory

Every mounted route touching the broker/account surface at base `d6ebeb24`,
accounted for. Dispositions:

- **catalog** — the operation joins the typed Alpaca catalog and gains a
  clerk-scoped public route under `/api/brokers/alpaca/clerks/{clerk_id}`.
  The agent keeps serving today's path verbatim (the `agent_path_template`);
  nothing the frontend or tooling calls today changes in B.
- **directory** — coordinator-owned surface built from the registry, not
  forwarded (PRD §10.1/§10.2).
- **retained-legacy** — stays unscoped for the compatibility window; the
  route-hit telemetry and consumer inventory that retire it are delivery E
  (D14). Zero hits on an unused deployment is not evidence.
- **coordinator-owned** — data-plane surfaces that belong to the coordinator
  role regardless of fleet state.
- **ibkr-legacy** — deprecated IBKR surfaces (AGENTS.md): documented, not
  developed, retired by the IBKR decommissioning track, never a fleet family.
- **examples** — developer fixtures, out of the product surface.

The catalog itself lives in
`PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py` and is the single
routing contract: coordinator forwarding allowlist, public route generation,
OpenAPI export and (with delivery C) the frontend builders all derive from it.

## Lane reads (catalog)

| Operation id | Method | Public (clerk-relative) | Agent path today | Capability | Readiness | Idempotency |
|---|---|---|---|---|---|---|
| `account_read` | GET | `/account` | `/api/brokers/alpaca/account` | `account_read` | execution | read |
| `positions_read` | GET | `/positions` | `/api/brokers/alpaca/positions` | `positions_read` | execution | read |
| `orders_read` | GET | `/orders` | `/api/brokers/alpaca/orders` | `orders_read` | execution | read |
| `market_status_read` | GET | `/market-status-snapshot` | `/api/brokers/alpaca/market-status-snapshot` | `market_status_read` | execution | read |

`market_status_read` lands in B because delivery A2 committed to it: the agent
serves market status and the coordinator forwards rather than 503ing when the
lane splits (A2 exit evidence, feed-dependency replacement).

## Configuration family (catalog)

All `configuration_access` readiness — a provisioned but unbound lane stays
servable so the operator can repair the configuration that would produce a
binding (D12). Capability `configuration_manage`.

| Operation id | Method | Public | Agent path today | Idempotency |
|---|---|---|---|---|
| `configuration_desk_state` | GET | `/configuration/desk-state` | `/api/brokers/alpaca/configuration/desk-state` | read |
| `configuration_owner_read` | GET | `/configuration/owner` | `/api/brokers/alpaca/configuration/owner` | read |
| `configuration_owner_update` | PATCH | `/configuration/owner` | `/api/brokers/alpaca/configuration/owner` | one_shot |
| `configuration_credential_slots` | GET | `/configuration/credential-slots` | `/api/brokers/alpaca/configuration/credential-slots` | read |
| `configuration_nicknames_read` | GET | `/configuration/account-nicknames` | `/api/brokers/alpaca/configuration/account-nicknames` | read |
| `configuration_nicknames_set` | PUT | `/configuration/account-nicknames/{account_id}` | `/api/brokers/alpaca/configuration/account-nicknames/{account_id}` | one_shot |
| `configuration_profiles_list` | GET | `/configuration/profiles` | `/api/brokers/alpaca/configuration/profiles` | read |
| `configuration_profile_create` | POST | `/configuration/profiles` | `/api/brokers/alpaca/configuration/profiles` | one_shot |
| `configuration_profile_read` | GET | `/configuration/profiles/{profile_id}` | `/api/brokers/alpaca/configuration/profiles/{profile_id}` | read |
| `configuration_profile_update` | PATCH | `/configuration/profiles/{profile_id}` | `/api/brokers/alpaca/configuration/profiles/{profile_id}` | one_shot |
| `configuration_profile_clone` | POST | `/configuration/profiles/{profile_id}/clone` | `…/{profile_id}/clone` | one_shot |
| `configuration_revisions_list` | GET | `/configuration/profiles/{profile_id}/revisions` | `…/revisions` | read |
| `configuration_revision_create` | POST | `/configuration/profiles/{profile_id}/revisions` | `…/revisions` | one_shot |
| `configuration_revision_read` | GET | `/configuration/profiles/{profile_id}/revisions/{revision}` | `…/revisions/{revision}` | read |
| `configuration_account_pin` | POST | `/configuration/profiles/{profile_id}/revisions/{revision}/account-pin` | `…/account-pin` | one_shot |
| `configuration_verify_account` | POST | `/configuration/profiles/{profile_id}/revisions/{revision}/verify-account` | `…/verify-account` | one_shot |
| `configuration_selection_read` | GET | `/configuration/selection` | `/api/brokers/alpaca/configuration/selection` | read |
| `configuration_selection_write` | PUT | `/configuration/selection` | `/api/brokers/alpaca/configuration/selection` | one_shot |
| `configuration_selection_apply` | POST | `/configuration/selection/apply` | `/api/brokers/alpaca/configuration/selection/apply` | one_shot |
| `configuration_events` | GET | `/configuration/events` | `/api/brokers/alpaca/configuration/events` | read |

Selection mutations are `one_shot`, not `durable_key`: they are fenced by the
clerk-local selection transaction (stage → apply → acknowledge, D9), which is
exactly the provider-side state fence the idempotency vocabulary describes.
Apply is the handover that advances `effective_binding_generation` only when
the effective tuple changes.

## Bot panel family (catalog)

Execution readiness; account-scoped (`requires_effective_account=True`; the
UUID target is cross-checked against the confirmed assignment before
dispatch — wrong-target refuses as `clerk_account_mismatch`, never retargets).

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `bots_catalog_read` | GET | `/accounts/{account_id}/bots/catalog` | `/api/brokers/alpaca/accounts/{account_id}/bots/catalog` | `bot_panel_read` | read |
| `bot_create` | POST | `/accounts/{account_id}/bots` | `/api/brokers/alpaca/accounts/{account_id}/bots` | `bot_action` | one_shot |
| `bot_admission_plan` | POST | `/accounts/{account_id}/bots/admission` | `…/bots/admission` | `deploy` | read |
| `bot_cohort_archive_read` | GET | `/accounts/{account_id}/bots/cohort-archive` | `…/bots/cohort-archive` | `bot_panel_read` | read |
| `bot_cohort_archive` | POST | `/accounts/{account_id}/bots/cohort-archive` | `…/bots/cohort-archive` | `bot_action` | durable_key |
| `bot_cohort_flatten_read` | GET | `/accounts/{account_id}/bots/cohort-flatten` | `…/bots/cohort-flatten` | `bot_panel_read` | read |
| `bot_cohort_flatten` | POST | `/accounts/{account_id}/bots/cohort-flatten` | `…/bots/cohort-flatten` | `bot_action` | durable_key |
| `bots_deploy_read` | GET | `/accounts/{account_id}/bots/deploy` | `…/bots/deploy` | `deploy` | read |
| `bots_deploy_apply` | POST | `/accounts/{account_id}/bots/deploy` | `…/bots/deploy` | `deploy` | durable_key |
| `bot_panel_read` | GET | `/accounts/{account_id}/bots/{sid}/panel` | `…/bots/{sid}/panel` | `bot_panel_read` | read |
| `bot_panel_action` | POST | `/accounts/{account_id}/bots/{sid}/actions` | `…/bots/{sid}/actions` | `bot_action` | durable_key |
| `bot_authority_facts` | GET | `/accounts/{account_id}/bots/{sid}/authority-facts` | `…/bots/{sid}/authority-facts` | `bot_panel_read` | read |
| `bot_chart_history` | GET | `/accounts/{account_id}/bots/{sid}/chart/history` | `…/bots/{sid}/chart/history` | `bot_panel_read` | read |
| `bot_chart_live` | GET | `/accounts/{account_id}/bots/{sid}/chart/live` | `…/bots/{sid}/chart/live` | `bot_panel_read` | read |
| `bot_evidence` | GET | `/accounts/{account_id}/bots/{sid}/evidence` | `…/bots/{sid}/evidence` | `bot_panel_read` | read |
| `bot_live_snapshot` | GET | `/accounts/{account_id}/bots/{sid}/live-snapshot` | `…/bots/{sid}/live-snapshot` | `bot_panel_read` | read |
| `bot_live_stream` | GET | `/accounts/{account_id}/bots/{sid}/live-stream` | `…/bots/{sid}/live-stream` | `bot_panel_read` | read (SSE) |
| `paper_access_plan` | POST | `/accounts/{account_id}/strategies/{program_key}/paper-access/plan` | `…/strategies/{program_key}/paper-access/plan` | `deploy` | read |
| `paper_access_confirm` | POST | `/accounts/{account_id}/strategies/{program_key}/paper-access/confirm` | `…/paper-access/confirm` | `deploy` | durable_key |

Plan-style POSTs (`admission`, `paper-access/plan`, manual `preview`) are
computation over durable state — repeats are free, so they carry `read`
idempotency, not a durable key. The panel's action body already carries
`idempotency_key` (`PanelActionRequest`); the §10.3 envelope key must agree
with it or the coordinator refuses before dispatch.

## Gallery family (catalog)

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `gallery_snapshot` | GET | `/accounts/{account_id}/gallery/snapshot` | `/api/brokers/alpaca/accounts/{account_id}/gallery/snapshot` | `gallery_read` | read |
| `gallery_stream` | GET | `/accounts/{account_id}/gallery/stream` | `…/gallery/stream` | `gallery_read` | read (SSE) |

## Desk lane reads (catalog, B2)

The canonical fleet desk may not re-resolve a global/default lane. These
public paths therefore pin the selected clerk before forwarding to their
existing Alpaca clerk handlers; the unscoped aliases remain telemetry-only
compatibility routes until Delivery E.

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `activities_read` | GET | `/activities` | `/api/brokers/alpaca/activities` | `account_read` | read |
| `portfolio_history_read` | GET | `/portfolio-history` | `…/portfolio-history` | `account_read` | read |
| `portfolio_history_proof_read` | GET | `/portfolio-history-proof` | `…/portfolio-history-proof` | `account_read` | read |
| `clerk_status_read` | GET | `/clerk/status` | `…/clerk/status` | `custody_read` | read |
| `custody_diagnosis_read` | GET | `/clerk/custody-diagnosis` | `…/clerk/custody-diagnosis` | `custody_read` | read |

## Bot run evidence (catalog, B2)

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `bot_run_current_read` | GET | `/accounts/{account_id}/bots/{sid}/runs/current` | `/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/current` | `bot_panel_read` | read |
| `bot_run_history_read` | GET | `/accounts/{account_id}/bots/{sid}/runs/history` | `/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/history` | `bot_panel_read` | read |

## Custody family (catalog) — new home for `/api/alpaca-clerk-sqlite` + `/api/accounts`

The clerk-scope custody prefix is the PRD's named home for these; the agent
paths stay exactly where they are today.

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `custody_account_snapshot` | GET | `/accounts/{account_id}/custody/snapshot` | `/api/alpaca-clerk-sqlite/accounts/{account_id}/snapshot` | `custody_read` | read |
| `custody_account_timeline` | GET | `/accounts/{account_id}/custody/timeline` | `…/timeline` | `custody_read` | read |
| `custody_bot_snapshot` | GET | `/accounts/{account_id}/custody/bots/{sid}/snapshot` | `…/bots/{sid}/snapshot` | `custody_read` | read |
| `custody_bot_timeline` | GET | `/accounts/{account_id}/custody/bots/{sid}/timeline` | `…/bots/{sid}/timeline` | `custody_read` | read |
| `custody_command_read` | GET | `/accounts/{account_id}/custody/commands/{command_id}` | `…/commands/{command_id}` | `custody_read` | read |
| `custody_runs_start` | POST | `/accounts/{account_id}/custody/bots/{sid}/runs/start` | `…/bots/{sid}/runs/start` | `custody_command` | durable_key |
| `custody_runs_stop` | POST | `/accounts/{account_id}/custody/bots/{sid}/runs/stop` | `…/bots/{sid}/runs/stop` | `custody_command` | durable_key |
| `custody_reconcile` | POST | `/accounts/{account_id}/custody/reconcile` | `…/reconcile` | `custody_command` | durable_key |
| `custody_recovery_check` | POST | `/accounts/{account_id}/custody/recovery-actions/check` | `…/recovery-actions/check` | `custody_command` | read |
| `custody_recovery_execute` | POST | `/accounts/{account_id}/custody/recovery-actions/execute` | `…/recovery-actions/execute` | `custody_command` | durable_key |
| `custody_bot_recovery_check` | POST | `/accounts/{account_id}/custody/bots/{sid}/recovery-actions/check` | `…/bots/{sid}/recovery-actions/check` | `custody_command` | read |
| `custody_bot_recovery_execute` | POST | `/accounts/{account_id}/custody/bots/{sid}/recovery-actions/execute` | `…/bots/{sid}/recovery-actions/execute` | `custody_command` | durable_key |
| `custody_historical_recovery_prepare` | POST | `/accounts/{account_id}/custody/bots/{sid}/historical-execution-recovery/prepare` | `…/prepare` | `custody_command` | durable_key |
| `custody_historical_recovery_confirm` | POST | `/accounts/{account_id}/custody/bots/{sid}/historical-execution-recovery/confirm` | `…/confirm` | `custody_command` | durable_key |
| `custody_transactions` | GET | `/accounts/{account_id}/custody/transactions` | `/api/accounts/{account_id}/transactions` | `custody_read` | read |
| `custody_transaction_read` | GET | `/accounts/{account_id}/custody/transactions/{transaction_id}` | `/api/accounts/{transaction_id}` (`/api/accounts/{account_id}/transactions/{transaction_id}`) | `custody_read` | read |
| `custody_external_order_ack` | POST | `/accounts/{account_id}/custody/transactions/external-orders/{external_order_id}/acknowledge` | `…/acknowledge` | `custody_command` | durable_key |
| `custody_pnl_attribution` | GET | `/accounts/{account_id}/custody/pnl-attribution` | `/api/accounts/{account_id}/pnl-attribution` | `custody_read` | read |

Recovery `check` POSTs are diagnostics over durable state (free repeats);
`execute` and the command paths are durable-keyed through the clerk's existing
command machinery, which remains the sole deduplication and outcome authority
(D11).

## Manual orders family (catalog)

| Operation id | Method | Public | Agent path today | Capability | Idempotency |
|---|---|---|---|---|---|
| `manual_orders_capability` | GET | `/accounts/{account_id}/manual-orders/capability` | `/api/brokers/alpaca/accounts/{account_id}/manual-orders/capability` | `manual_orders` | read |
| `manual_orders_preview` | POST | `/accounts/{account_id}/manual-orders/preview` | `…/manual-orders/preview` | `manual_orders` | read |
| `manual_order_ticket_read` | GET | `/accounts/{account_id}/manual-order-tickets/{ticket_id}` | `…/manual-order-tickets/{ticket_id}` | `manual_orders` | read |
| `manual_order_ticket_put` | PUT | `/accounts/{account_id}/manual-order-tickets/{ticket_id}` | `…/manual-order-tickets/{ticket_id}` | `manual_orders` | one_shot |
| `manual_order_ticket_cancel` | POST | `/accounts/{account_id}/manual-order-tickets/{ticket_id}/cancel` | `…/cancel` | `manual_orders` | durable_key |
| `manual_order_ticket_continue` | POST | `/accounts/{account_id}/manual-order-tickets/{ticket_id}/continue` | `…/continue` | `manual_orders` | durable_key |
| `manual_order_cancel` | POST | `/accounts/{account_id}/manual-orders/{order_ref}/cancel` | `…/manual-orders/{order_ref}/cancel` | `manual_orders` | durable_key |

Tickets are durable identities on the clerk (`DurableConflictError` on key
reuse with different input); PUT is one_shot keyed by `ticket_id`, and the
path-style `order_ref` parameter rides the catalog's `:path` form.

## Directory (coordinator-owned)

| Route | Source |
|---|---|
| `GET /api/broker-clerks` | Registry projection across production providers (§10.1 shape) |
| `GET /api/brokers/{broker}/clerks` | Same projection filtered to one broker |
| `GET /api/brokers/{broker}/clerks/{clerk_id}` | One lane's lifecycle, session, capabilities, provider summary |

## Retained-legacy (unchanged in B; retirement is E)

Unscoped reads the compatibility window keeps, per D14 — each row keeps its
consumers until the route-hit telemetry and consumer inventory say otherwise:

- `broker_bots` — `/api/brokers/{broker}/bots` (list/create/stop, runs
  current/history, instance read).
- `run_replay` — `/api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt`.
- `broker_v2_panel` unscoped aliases — `/api/brokers/{broker}/bots/catalog`,
  `/bots/{sid}/(panel|actions|chart/*|evidence)`, `/panel-profile`.
- `brokers` lane extras — `/api/brokers/{broker}/(assets|activities|clock|`
  `order-groups|portfolio-history|portfolio-history-proof|live-verdict|`
  `fees/session-reconciliation|live-envelope/loss-hold|clerk/status|clerk/custody-diagnosis)`.
- `/api/brokers/alpaca/configuration/**` direct (unscoped) — stays until the
  frontend cutover (C) moves consumers to the clerk-scoped surface.

None of these gain new behavior in B; none is developed further.

## Coordinator-owned (not fleet families)

- `broker_capability` — `/api/broker/capability(/probe)`: control-plane
  capability surface.
- `broker.data-plane/health` — coordinator health.

## IBKR-legacy (deprecated — AGENTS.md)

`/api/broker/health`, `/connect`, `/disconnect`, `/reconnect`, bars snapshots,
expirations/strikes/option-chain/option-contracts/option-surface, ibkr
evidence (+stream). Documented here only so the inventory is complete; they
are outside the fleet and outside new development.

## Examples

`/api/examples/alpaca-bot-control/fixtures` — developer fixtures.

## Contract consequences

- Capability vocabulary widens in code (the enum's documented review process):
  `market_status_read`, `deploy`, `custody_command`, `manual_orders`.
- §10.3 command envelope applies to every non-`read` catalog operation:
  `capability` must match the operation's, `idempotency_key` is required for
  `durable_key` operations, `expected_effective_binding_generation` pins the
  fence, and `target.account_id` must equal the confirmed assignment.
- §10.4 refusal families are the fleet error hierarchy already carrying
  pinned statuses; the coordinator router maps them 1:1 and adds nothing.
- Every stream operation carries per-event identity and closes on provenance
  violation (FR-076) — the A2 review's deferred findings land here.
