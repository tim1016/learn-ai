# Fleet Trust Register — the final eight

> Successor to `2026-09-14-fleet-trust-register-closeout.md`. That document sequenced ten
> issues; two have closed since. This one sequences the last eight, and each unit below is
> now a dispatchable work-order issue.

**Written:** 2026-09-14 18:25 CDT against `origin/master` `46948353`.
**Revised:** 2026-09-14 19:10 CDT — an independent audit corrected the deploy premise (§1)
and four `file:line` citations (§2a). Both corrections are folded in below.

---

## 0. State verified at writing time

| Fact | Value |
|---|---|
| `origin/master` | `46948353` — equals main-checkout HEAD, 0 ahead / 0 behind, tree clean but for untracked `.zcode/` |
| **Deployed** | **Everything.** See §1 — there is no deploy debt. |
| Open PRs | none |
| Problem issues | **8** — #2066, #2067, #2069, #2070, #2072, #2075, #2076, #2080 |
| Work orders | **8** — #2097–#2104, labelled `ready-for-agent` |
| Follow-ups filed | **9** — #2105–#2113 |
| Closed since the close-out | **#2068** (PR #2096), **#2074** (#2093, #2094), **#2095** (owner-waived) |
| Stray `fleetqualification*` volumes | **3**, from the failed 2026-09-13 run |
| Market | Monday 2026-09-14, RTH closed, no bot running |

### The close-out document is stale in four places

| It said | Now |
|---|---|
| Track A (G-3b) "ready now" | **Done.** #2093 + #2094 merged; #2074 closed. |
| Track B "Lane E was never planned" | **Done.** The lane plan exists: 12 tasks + 1 deferred. |
| E-A "blocked: Lane E plan missing" | **Done.** PR #2096 merged; #2068 closed. |
| "no restart is owed" | **Still true** — for a different reason than it gave. See §1. |

The handoff dated today is stale in the same three ways, plus it describes E-A as in flight
with two unresolved thermo blockers. Both were fixed (`9a2afca2`, `a9f06298`) and #2096 merged.

---

## 1. There is no deploy debt — correcting this document's first draft

**The first draft of this plan claimed E-A was merged but not running, and recommended a
restart tonight to close the #2068 hole. That was wrong.** It inferred a frozen image from the
container start time (12:17 CDT, before the merge) without checking the serve mode.

Measured:

- `my-frontend` runs `npx ng serve --host 0.0.0.0 --poll 2000` with
  `/Users/inkant/learn-ai/Frontend/src` **bind-mounted** to `/app/src`.
- Local master was pulled to `46948353` at 18:09:33 CDT; the container logged
  `Application bundle generation complete` at **18:09:43 CDT**. The watch rebuilt on the pull.
- **E-A's fence is live in the browser now.** #2068 is closed *and* deployed.
- Every post-`cec92fcf` Python commit is a test-only sweep (#2093, #2094), and the override
  mounts only `app/` — so tests never reach a running lane.

**Consequence: no window is owed until the next backend PR lands.** Restarts are owed in the
future by D-C, D-B, D-D, E-B and the audit route (#2104), plus the #2066 migration. E-C and E-D
owe nothing — watch-mode serves them.

The general lesson is worth more than the correction: **a running container's start time says
nothing about the code it is executing** when the source is bind-mounted under a watcher. Check
the command and the mounts, not the uptime.

---

## 2. Work orders

Each row is a self-contained issue labelled `ready-for-agent`, sized for one agent and one PR.

| Wave | Issue | Unit | Closes | Regenerates contracts | Restart |
|---|---|---|---|---|---|
| **W1** | **#2097** | **D-C** — clerk agent refuses unpinned mutations, then CONTEXT.md truth | #2075 | no | yes |
| **W1** | **#2098** | **F-1** — commit the running topology, secret-free, + render gate | advances #2066 | no | no |
| **W2** | **#2099** | **D-B** — delete 3 dead unscoped mutations + absence contract | #2069 | **yes** | yes |
| **W2** | **#2100** | **D-D** — 7 backend cleanups + agent-path contract test | advances #2076 | **yes** | yes |
| **W3** | **#2101** | **E-B** — refusal vocabulary, `next_step` backfill, one wire shape | advances #2067 | **yes** | yes ⚠️ |
| **W3** | **#2102** | **E-C** — frontend learns the vocabulary; 409 stops reading "Unknown" | #2067 | no | no |
| **W4** | **#2103** | **E-D** — catalog-derived `operationUrl`; delete `listOrderGroups` | #2076 | **yes** | no |
| **W5** | **#2104** | **Audit read surface** — one secret-gated coordinator GET | #2072 | **yes** | yes |

**Owner-only, no code, no work order:**

| Action | Closes | Window |
|---|---|---|
| Enable IB Gateway auto-restart (Configure → Settings → Lock and Exit → Auto restart) | **#2080** | any time — the only remaining work on it |
| Reclaim the 3 `fleetqualification*` volumes, then the one-time fault-matrix run | **#2070** | ~30 min, no bot |
| Copy `compose.override.yaml` to 0600 **outside the repo** | prereq for #2066 | before F-1's migration |
| Topology migration M1–M9 | **#2066** | ~45 min, no bot, after #2098 |

### 2a. Measured line-number drift — do not trust the plans blind

The Lane D plan predates `cec92fcf`; the Lane E plan was verified at `cec92fcf`; E-A and the
Lane G sweeps landed after both. Confirmed current positions on `46948353`:

| Symbol | Plan says | **Measured** |
|---|---|---|
| `_SAFE_METHODS` (`lane_runtime.py`) | `:31` | **`:35`** |
| `bots_deploy_apply` (`fleet_adapter.py`) | `:344` | **`:340`** |
| `list_routing_receipts` (`store.py`) | `:832` | **`:853`** |
| `production_adapter` (`provider.py`) | `:348` | **`:350`** |

Verified unchanged: `CONTEXT.md:2121`/`:2124`, `_ADAPTER_VERSION` at `fleet_adapter.py:25`,
the bare-detail refusal sites, the export-check gate at `ci.yml:272`, and E-A's three new
frontend files. **Re-verify every `file:line` at your own branch point regardless.**

---

## 3. Parallelism — what actually runs concurrently

Authoring parallelises freely. **Merging does not**, for exactly one reason: five work orders
regenerate `contracts/openapi/python-data-service.openapi.json` and
`Frontend/src/app/api/broker.types.ts`. **Never two regenerating PRs in flight unrebased.**

```
W1  #2097 D-C ─────────────────────────────────────────────► #2075 ✓
    #2098 F-1 ────────────────► owner migration ───────────► #2066 ✓
                                                    (both start today, share no file)

W2  #2099 D-B ──► #2100 D-D ──┬──► #2101 E-B ──┬──► #2103 E-D ──► #2104 ──► #2072 ✓
      │             │         │                │        └─────────────────► #2076 ✓
      │             │         │                └──► #2102 E-C ────────────► #2067 ✓
      │             └─────────┴─ the regeneration chain: strictly serial at merge
      └──────────────────────────────────────────────────────────────────► #2069 ✓
```

**Three agents can run today:** #2097, #2098, and whoever authors #2099 against master.
`#2102` (E-C) is frontend-only and can be authored in parallel with `#2103` — it merges after
`#2101` because it imports that PR's committed snapshot.

**The `ci.yml` rebase chain has four members: `#2078 → #2098 → #2101 → #2103`.** A missed
rebase here **silently drops a gate** rather than failing. Name the chain in every PR body.

---

## 4. Deploy windows

| # | What | When |
|---|---|---|
| — | *(nothing owed today)* | — |
| **W-a** | Batched restart for D-C + D-B + D-D + E-B | after those four merge |
| **W-b** | Audit route (#2104) | with W-a or after |
| **W-c** | Topology migration M1–M9 | after #2098, ~45 min |
| **W-d** | Containment hardening — read-only rootfs, tmpfs, `pids_limit`, `fleet-private` | **≥1 full trading day after W-c**, separate PR |

Every `./restart.sh` is an unconditional `podman compose down` (`restart.sh:21`) — both clerks
drop. Windows: after 15:00 CT on a trading day, before the first launch, or a weekend.
**Batch W-a rather than restarting per PR.**

---

## 5. Follow-ups — filed, not carried

A plan document is not a work queue. All nine are now issues:

| Issue | What |
|---|---|
| **#2105** | **Fleet tokens printed into an agent transcript — rotate.** Values are in one conversation's tool output only; nothing written, committed or pushed. Internal fleet credentials on the private compose network. Includes the process fix: name-only extraction, never a bare `cat` on a credential file. |
| **#2106** | The desk-scoped command fence is missing — four components read their target at interaction time, and the freeze contract spec **cannot see them** (they contain no literal `fleetDirectory.lane(`). |
| **#2107** | Four hand-built raw-ASGI refusal writers bypass `detail()`; E-B's snapshot pins the code but not the body. |
| **#2108** | The coordinator's two unscoped reads are absent from the exported OpenAPI (the exporter forces `combined`; the routes mount coordinator-only), so the frontend's panel-profile call is typed against the wrong mount. |
| **#2109** | `broker-configuration.service.ts` — 17 URLs off a prefix the catalog never declares; the one write path that can change which account a lane serves. |
| **#2110** | `getLiveVerdict` is a lane-implicit shell poll; with two lanes it shows one lane's verdict as the installation's. Needs a backend decision first. |
| **#2111** | `draining` is unreachable — needs a drain ceremony, not a cleanup. |
| **#2112** | `_PRIVATE_HOST_CACHE` has no TTL; changing it touches a boundary check on the live credential path. |
| **#2113** | Four Codex P2 follow-ups carried from the merged lanes. |

**Not debt, by owner ruling:** `alpaca-deploy-workflow.component.ts` at 1017 lines. The ~1k
file-size rule is waived for this file and #2095 was closed on that instruction. Narrowly-scoped
additions are fine. **Do not re-raise it.**

---

## 6. Decisions

| # | Question | Default |
|---|---|---|
| ~~1~~ | ~~Deploy E-A tonight?~~ | **Void** — it is already live (§1). |
| 1 | **Rotate the five fleet token keys** (#2105)? | **Yes** — cheap, internal, and it ends the question. |
| 2 | Build the audit route (#2104) this round, or leave #2072 filed? | **Build it**, last in the chain. |
| 3 | Delete the dead `listOrderGroups` (decision 13), or bridge it? | **Delete.** The bridge path is written down if you flip. |
| 4 | Dispatch #2097, #2098 and #2099 to three agents now? | **Yes** — they share no file. |

---

## 7. Delivery protocol

Failing test verified **on master first** → `ruff check PythonDataService/app/
PythonDataService/tests/` → targeted suites **plus every suite consuming a shared helper you
edited** → **one** independent thermo-nuclear review with mutation proofs and red/green counts →
push → 30 min for CI + CodeRabbit → fix → merge. Re-pushes do not re-trigger thermo.

**Baselines to re-establish at your branch point** (do not inherit these numbers):

| Suite | Command | Last measured |
|---|---|---|
| Frontend (host, from worktree) | `npx ng test --watch=false`, `eslint Frontend/src/ --max-warnings 0` | 285 files / 2578 tests |
| Python fleet + contracts | `DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/contracts -q` **unsandboxed** | 342 at `cec92fcf`; re-measure |
| `tests/broker/v2panel` | same | **5 inherited env-only failures** (`POSTGRES_URL` empty) — never yours |

Constraints binding every task are inlined in each work-order issue. The load-bearing ones:
never `checkout`/`switch`/`stash`/`pull` in `/Users/inkant/learn-ai`; never disable
`IBKR_BROKER_ENABLED`; `tests/broker/fleet` binds sockets and **fails** under the sandbox;
frontend tests run host-side from the worktree (`podman exec my-frontend ng test` grades the
main checkout); docs edits pass `pytest tests/contracts`; **secrets are key names only, ever.**

---

## 8. Housekeeping

Worktrees `fleet-lane-d` (`4a8a8cf4`) and `fleet-lane-e1` (`a9f06298`) are both merged into
master and prunable — **but copy their `.superpowers/sdd/` ledgers out first.** The Lane E
ledger, including `thermo-fix-report.md` (the definitive account of E-A's review findings and
mutation proofs, and the source of the precedent quoted in every work order), lives inside
`fleet-lane-e1` and dies with the worktree.
