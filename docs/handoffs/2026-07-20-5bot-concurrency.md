# Handoff — Concurrent-bot (5-bot cohort) research, 2026-07-20

**For:** a fresh agent (or the user) continuing the "run N concurrent bots" work on
learn-ai in a new session.
**Repo:** `/Users/inkant/learn-ai` (branch `master`; user authorized committing directly
to master this session — they turn the commits into a PR later).

---

## 1. Mission (unchanged) + the reframe

Run **multiple bots concurrently** on the IBKR **paper** account **DUM284968**, then a
**UI-driven crash-and-recover** test (Phase 2, not started). Original target = 5 concurrent
via a certified cohort. **User's late reframe (important):** *concurrency matters, session
duration/runway does NOT.* So a short-overlap run that simply gets 5 bots **up at once** is
a win — do not chase the 45-min certificate window or the 15:55 ET force-flat.

**Status:** peaked at **4/5 concurrent** live. Two real bugs fixed; the **true**
concurrency ceiling is now diagnosed but **not yet fixed** (see §3). Everything durable is
committed + in memory.

---

## 2. Do not re-derive these — read them first

- **Design doc / full evolving diagnosis:** `docs/architecture/2026-07-20-concurrent-cohort-reconciliation-hardening.md`
  (root causes A, B, C-true, adjacent issues, fixes, slices). **Read this first — especially
  the "Architectural blindspots — higher-angle review" section (commit 0593da394):** the
  meta-finding is that A/B/C are symptoms of a single-bot-first design; the per-bot
  transaction identity (order_ref namespace) already exists but was never made
  *authoritative*, and the Clerk account authority exists but enforcement + read paths
  bypass it. That section contains the sequenced target shape (steps 1–4) that the next
  session's work should follow; step 1 = §3 below.
- **Memory (auto-loaded):** `project_five_bot_cohort_and_crash_recovery.md` — program state,
  all 4 run attempts, operational gotchas, 9-commit list. Plus linked memories:
  `project_live_fleet_reconciliation`, `feedback_flag_readonly_gotcha`,
  `project_dataplane_host_vs_container_control`, `project_python_service_hot_reload_broken`.
- **Prerequisite commits:** branch `handoff/5-concurrent-cohort-base` (also on local master). Highlights:
  `aed255536` 5-bot profile · `e7ec572b3`/`1d1f0c594` UI+preset · `62ffb97d1` slot retry ·
  `7e22f1dce` **Fix A** · `357609b9d` probe-timeout (symptom band-aid) ·
  `2320da708`/`9a4b5da5a`/`b34e255ac` design+diagnosis docs.
- **Runbook:** `docs/runbooks/cross-client-execution.md` (the outside_mutation class).
- **Monitor script (reusable):** `PythonDataService/scripts/cohort_monitor.py` — polls the latest cohort;
  `until_all_up` mode exits on ALL_UP(0) / MEMBER_BLOCKED(1) / MEMBER_DROPPED(2, crash) /
  TIMEOUT(3). Run: `PythonDataService/.venv/bin/python PythonDataService/scripts/cohort_monitor.py until_all_up`
  as a background task.

---

## 3. THE next thing to do: fix the true concurrency blocker (root cause C-true)

**Diagnosis (solid, evidence in the design doc):** the fleet read paths — `/catalog` and
the **roll-call** — fire repeated IBKR `reqPositions` **per bot**, serializing on the single
broker connection → **O(N bots) → ~12 s at 6 bots**. That makes the roll-call too slow to
offer a member at its staggered slot, so it's **silently dropped** (roll-call skips
ineligible bots at the `if not status_is_roll_call_eligible(...): continue` line). Ruled out
(all fast, <0.2s): daemon `/instances`, container→host hop, volume I/O, journal parsing,
daemon in-memory bloat, fleet-contamination compute.

**Recommended fix:** route the read paths through the **cached** Account Truth snapshot (the
15 s `AccountTruthRefreshLoop` in `main.py` already maintains one) instead of triggering a
fresh per-bot `reqPositions`; compute account-level state (truth/exposure/fleet-contamination)
**once per request** and share across bot rows. Files: `app/routers/live_instances.py`
(`list_bot_catalog`, `run_roll_call`, `_bot_catalog_row_for_sid`, `_resolve_fleet_blocks_starts_for_status`),
`app/services/account_truth_snapshot.py`, `app/services/fleet_contamination_service` /
`_compute_account_fleet_contamination`. **CAUTION:** `live_instances.py` is a **frozen
>1k-line router** (`.claude/rules/python.md` — net physical lines may not increase; offset
any addition with a same-PR extraction). Prefer putting new logic in a service module.
**Expected result:** `/catalog` + roll-call drop to sub-second → roll-call offers all members
→ **5 concurrent reachable**.

**Verify with:** time `/catalog` before/after (should go ~12 s → <1 s), then re-run a 5-bot
cohort (short overlap is fine) and watch for ALL_UP.

---

## 4. What's already fixed / validated (don't redo)

- **Fix A (VALIDATED LIVE, `7e22f1dce`):** a bot no longer fatal-halts on its **own** fill.
  `check_outside_mutation` now recognizes a fill whose echoed `order_ref` is in the run's own
  namespace as owned (3rd signal beside client_order_id/perm_id), closing the perm-id race.
  Proven: `cohort5-msft2` survived where `cohort5-msft` crashed. **Don't touch.**
- **Slot-preflight retry (`62ffb97d1`)** and **probe timeout (`357609b9d`)** — both help but
  are **band-aids** for C; the §3 fix supersedes them.

---

## 5. Adjacent issues found (report/fix opportunistically)

1. **`policy_blocks_starts=True`** currently — residual fleet contamination from today's runs
   would **block a new cohort launch** until reconciled/cleared. Check + clear before the next
   launch (account verdict was CLEAN, but fleet policy blocks — investigate the mismatch).
2. **Unbounded never-compacted journals** re-parsed every read: `_broker/session_roster_history.jsonl`
   **58 MB**, `accounts/DUM284968/clerk_journal.jsonl` **8.6 MB**, `account_events.jsonl` 3.8 MB.
3. **Per-run `host_daemon.log` = 24 MB** (daemon dumps every execDetails/commission/position).
4. **N+1 fleet contamination** — recomputed per bot, no memoization.
5. **Fix B (open, delicate):** runtime `outside_mutation` is not sibling-aware on a shared
   account (only cold-start reconciliation is). See design doc.
6. **Alternative to A/B entirely:** one IBKR paper account per bot (needs more DU accounts).

---

## 6. Operating the live stack (practical)

- **Topology:** host daemon (`:8765`, must be host — IBKR error-420 same-IP binding) + host
  account clerk (gen ~50, account DUM284968) + **container** data-plane (`:8000`,
  `polygon-data-service`) + frontend (`:4200`, `my-frontend`). IB Gateway **paper** on `:4002`
  (4001=live; verify it's 4002).
- **Container data-plane CAN drive the full flow** (deploy via daemon passthrough of
  `qc-audit-copies`; the old "references/ not mounted" blocker is resolved). Only direct
  clerk-RPC (cures/flatten) needs a host process.
- **Control endpoints need a header:** `SECRET=$(podman exec polygon-data-service printenv
  DATA_PLANE_CONTROL_SECRET)` then `-H "X-Data-Plane-Control-Secret: $SECRET"`. The browser
  proxy injects it automatically.
- **Hot-reload is broken** (macOS+podman): after editing data-plane code,
  `podman restart polygon-data-service`. Bot subprocess code (`live_engine`/`halt`) loads
  fresh on each spawn (no restart needed). Daemon code needs `./bootstrap-host-daemon.sh --restart`.
- **Deploy a bot (UI `/broker/deploy`):** name → Strategy "Deployment Validation" → signal
  symbol → add ON-ENTER stock leg via ticker search (pick NASDAQ/ARCA primary) → SIZING "One
  share per signal" (=FixedShares 1) → LAUNCH SETTINGS **"Paper orders"** (= submit-to-paper;
  "Read-only"=observe, "Live"=blocked) → **"Prepare for cohort"** (deploys WITHOUT starting).
- **Launch a cohort:** `/broker/bots` → "Select ready cohort" → "Select 5-bot stagger preset"
  → Authorize. Or API: `POST /api/live-instances/accounts/DUM284968/cohort-launch`
  `{"member_strategy_instance_ids":[...5...],"launch_profile":"paper_five_bot_stagger_v2"}`.
- **Cohort ≈ 1 restart-intensity group** (won't trip the 3-starts/5-min freeze). Crash
  restarts are excluded from the gate.
- **Current 5 deployable bots:** `cohort5-aapl/nvda/qqq/spy` + `cohort5-msft2` (fresh MSFT
  replacement; original `cohort5-msft` is poisoned/`STOPPED_REQUIRES_REDEPLOY`, leave it).
- **Env now:** account DUM284968 **CLEAN + flat**, all bots stopped, git tree clean, monitor
  processes killed. A `pgrep -f cohort5_monitor.py` should be empty.

---

## 7. Cheap unblock to try first (before/with the §3 fix)

The read path also scans **20 accumulated run dirs** (today's + prior sessions). Pruning the
old/retired run dirs (keep the 5 current `cohort5-*` runs' dirs) may reduce the per-bot work
and is worth timing `/catalog` before/after. But §3 (per-bot IBKR serialization) is the
dominant cost and the real fix.

---

## 8. Prioritized next steps

1. **Build the §3 fix** (cached account-truth in read paths) — highest leverage; likely
   unlocks 5-concurrent. TDD; mind the frozen router.
2. **Re-run a 5-bot cohort** (short overlap OK per the reframe) → confirm ALL_UP / 5 concurrent.
3. Then **Phase 2**: UI-driven crash-and-recover test (design in memory; retire-and-replace flow).
4. Opportunistically: journal compaction, log verbosity, N+1 memoization, Fix B / one-account-per-bot decision.
5. Before the user PRs the 9 commits: run **thermo-nuclear-code-quality-review** (already run
   once this session — PASS + 1 applied simplification); note **148 pre-existing frontend
   eslint warnings** in untouched files (inherited debt, surface in PR).

---

## Suggested skills

- **`systematic-debugging`** — for building/verifying the §3 fix (it's a perf/behavior bug).
- **`test-driven-development`** — Fix §3 and any new fix ships red→green (repo mandates a
  regression test per bug fix).
- **`verification-before-completion`** — before claiming the concurrency fix works, verify
  live (time `/catalog`, watch a cohort reach ALL_UP).
- **`thermo-nuclear-code-quality-review`** — before the user opens the PR from the 9 commits.
- **`brainstorming`** — if reconsidering the architecture (shared-account vs one-account-per-bot).


---

> **Superseded planning note:** §3 and §8 of this handoff are now formalized (and tightened) as **PRD #1136 rev 2** — that issue is the authoritative spec; this document remains the operational playbook.
