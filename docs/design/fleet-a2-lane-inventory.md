# Fleet A2 — runtime-root and dependency inventory (role assignment)

Status: accepted with delivery A2 (ADR 0062 addendum; audit 2026-09-13 finding 5).
Scope: one existing Alpaca lane, split into `fleet_coordinator` and `clerk_agent`
roles with `combined` preserving today's process exactly. This inventory assigns
every writer, process global, credential set, background task, operational root
and market-data dependency to exactly one role before any composition change.

Verified against `app/main.py`, `app/broker/alpaca/**`, `app/broker_configuration/**`,
`app/services/**`, `app/broker/ibkr/config.py` and `compose.yaml` on the A2 branch.

##Writable roots

| Root | Source setting | Written by | Role in fleet mode |
|---|---|---|---|
| `<ALPACA_CLERK_DIR>/broker_configuration/profiles.db` | `ALPACA_CLERK_DIR` | profiles store, worker binding | **clerk_agent** — the lane's configuration authority (ADR 0060) |
| `<ALPACA_CLERK_DIR>/accounts/alpaca/**` (clerk.db, mirrors, activations, registries, recovery locks, arming/shadow/synthetic fences) | `ALPACA_CLERK_DIR` | custody repository, activation/arming stores | **clerk_agent** |
| `<ALPACA_CLERK_DIR>/daily_sovereign_equity_snapshots.sqlite3` | `ALPACA_CLERK_DIR` | sovereign equity scheduler | **clerk_agent** (starts only with a live binding) |
| `<live_artifacts_root>/live_state/**` (bot bindings, lifecycle state, identity guard, boot recovery, replay proofs) | `IBKR_LIVE_RUNS_ROOT` (parent) | `BotTaskRegistry` and its repositories | **clerk_agent** — re-homed inside the clerk volume (`<clerk_dir>/live_runs`) |
| `<live_artifacts_root>` arming/seal reads (roster seals, live verdict, arming admission, prior obligations) | `IBKR_LIVE_RUNS_ROOT` (parent) | Alpaca clerk callbacks + routers | **clerk_agent** (same re-homing; the coordinator forwards, never reads) |
| `<IBKR_LIVE_BARS_ROOT>` (aggregated live bars) | `IBKR_LIVE_BARS_ROOT` | `LIVE_BAR_AGGREGATOR` | **clerk_agent** — re-homed inside the clerk volume (`<clerk_dir>/live_bars`); derived data, rebuilt on loss |
| Data lake + LEAN caches + research artifacts | `LEAN_*`, lake settings | research/data-plane jobs | **fleet_coordinator** (shared read datasets stay coordinator-owned; agents mount them read-only when a lane needs them) |
| Broker REST/stream captures | `BROKER_CAPTURE_DIR` (default `<service>/var/broker_captures`) | capture journal | **clerk_agent** (per-lane container filesystem; capture is lane evidence) |
| Coordinator fleet registry | `FLEET_CONTROL_DIR` (new) | `FleetRegistryStore` | **fleet_coordinator** — its own control volume; contains no lane data (FR-023) |

The clerk-agent role enforces the re-homing as a startup fence, not a convention:
when the role is `clerk_agent` (or a fleet-enrolled `combined`), every writable
root above must resolve inside the verified clerk volume root, and a root that
escapes it refuses before any database writer or broker client opens.

## Process globals

| Global | Owner role | Notes |
|---|---|---|
| `ActiveClerkRuntime` (`set_active_clerk_runtime`) | **clerk_agent** | one per agent process; the coordinator constructs none (FR-041) |
| Active Alpaca binding (`set_active_alpaca_binding`) | **clerk_agent** | |
| `TradeUpdatesConsumer`, `AlpacaMarketLivenessConsumer` singletons | **clerk_agent** | per-lane streams |
| `process_repositories` cache + `close_all_repositories` | **clerk_agent** | cross-process exclusion is the custody execution lease, unchanged |
| Live projection hubs (`_HUBS`) | **clerk_agent** | panel/gallery streams are lane-scoped |
| `LIVE_BAR_AGGREGATOR` | **clerk_agent** | bars come from the shared feed (below); the root is lane-local |
| `BotTaskRegistry` | **clerk_agent** | bots couple to the lane's recovery/sweep seams |
| IBKR client/monitor/feed globals | **clerk_agent** (dependency-only) | see market-data decisions; the coordinator role leaves them unset and the v1 IBKR routers stay unmounted in fleet roles |
| Jobs/progress (Redis) | **fleet_coordinator** | `fail_jobs_without_a_worker` assumes the coordinator process owns the listener |

## Credentials

| Credential | Role | Notes |
|---|---|---|
| Broker credential slots (`ALPACA_API_KEY_ID/SECRET`, `ALPACA_CREDENTIAL_LIVE_*`) | **clerk_agent** only | agents get exactly their lane's slots; the coordinator process never needs broker credentials |
| `DATA_PLANE_CONTROL_SECRET` (browser installation secret) | **fleet_coordinator** terminates it | never forwarded to an agent (FR-046) |
| Per-clerk `worker_key` + two `svct_` transport tokens | agent + coordinator respectively | env-file convention; registry stores only the worker key |

## Background tasks

| Task | Role |
|---|---|
| Boot recovery sweep → reconciliation sweep (order preserved) | **clerk_agent** |
| Lease heartbeat (`start_lease_heartbeat`) | **clerk_agent** |
| Envelope sync + hold sync (`start_background_taps`) | **clerk_agent** |
| Sovereign equity scheduler | **clerk_agent** |
| Loop-lag watcher | both (process-local) |
| Agent fleet heartbeat (new) | **clerk_agent** |
| Research jobs / data-plane listeners | **fleet_coordinator** |

## Market-data dependency decisions

1. **Shared market-data feed (IBKR-owned `get_market_data_feed`).** The bot
   runner resolves live bars through this feed even for Alpaca bots. It is a
   retained read-only dependency of the clerk-agent role — physically present,
   unchanged — and explicitly *not* a development surface for the deprecated
   IBKR broker-control product (AGENTS.md). In `clerk_agent` role the feed
   globals may be constructed (feed-only); the v1 IBKR broker routers
   (`/api/broker/**`) mount only in `combined`. In `fleet_coordinator` role the
   feed stays unset and Alpaca bot deploys are not served there.
2. **Market-status upstream (`ALPACA_MARKET_STATUS_UPSTREAM_URL`).** The paper
   twin topology is superseded by the fleet: the liveness consumer belongs to
   the agent that owns the lane, so the upstream URL is not set in fleet roles.
   The supported replacement for the coordinator-side
   `/api/brokers/alpaca/market-status-snapshot` read is the clerk-scoped
   forwarding route (delivery B) — the coordinator forwards to the serving
   agent rather than 503ing. Until B lands, the legacy route stays mounted in
   `combined` only.
3. **Live bars aggregation.** `LIVE_BAR_AGGREGATOR` moves wholesale into the
   agent (root re-homed per the table); the coordinator keeps no bar state.

## Routers by role

| Router family | combined | fleet_coordinator | clerk_agent |
|---|---|---|---|
| Data-plane core (health, research, engine, lake, aggregates, jobs) | mounted | mounted | not mounted |
| `brokers` v2 reads + manual orders | mounted | not mounted (B adds clerk-scoped forwards) | mounted (lane-local) |
| `broker_configuration` | mounted | not mounted (B forwards) | mounted |
| `broker_v2_panel`, `broker_v2_gallery`, `broker_bots`, `run_replay`, `alpaca_clerk_sqlite`, `clerk_transactions`, `account_pnl_attribution` | mounted | not mounted (B forwards) | mounted |
| v1 IBKR `/api/broker/**` | mounted (legacy posture) | not mounted | not mounted |
| Internal `/internal/fleet/**` (new) | mounted | mounted | not mounted (agents call it as clients) |

`combined` remains byte-for-byte today's process: legacy unscoped routes stay
during the measured compatibility window (ADR 0062 addendum; retirement is
delivery E).
