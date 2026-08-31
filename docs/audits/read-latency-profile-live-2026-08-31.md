# Panel/Catalog Read-Latency Profile — the live half (#1801), 2026-08-31

**Scope.** The live measurements `#1801` still owed after the offline
profile (`read-latency-profile-2026-08-31.md`, PR #1907) landed: the
container filesystem A/B, O2 deploy latency under fleet load, the S12d
post-mass-stop CPU residue, F13's 10-concurrent case on the deployed
topology, and re-running the bench in-container. Measured on the deployed
split (data plane in `polygon-data-service`, Alpaca paper account
`PA3KWXU1C4C3`, SQLite clerk sole authority) with a real 50-bot trading
fleet staged for this purpose.

**Headline.** The offline profile's central puzzle — a ~100× "idle
multiplier" that the virtiofs bind mount was hypothesised to explain — was
an **artifact of comparing unlike measurements**. The 2026-08-26 audit's
T2/O4 figures come from `scripts/dev/fleet/read_bench.py`, a *concurrent*
6-thread read storm; the offline profile compared them against
*sequential* single-request bench numbers. Corrected to a
concurrent-to-concurrent comparison, the live numbers follow from the
serialization mechanism the offline profile already established. **No
filesystem effect, no lock, and no history-depth effect is needed to
explain any observed figure.**

## 1. The comparison error, and why it matters

`docs/audits/read-latency-profile-2026-08-31.md` §3 states: *"The live idle
3.3 s (94 rows) is ~118× this host's 28 ms."* The two sides are not
comparable:

| | audit's 3.3 s | offline profile's 28–30 ms |
|---|---|---|
| concurrency | **6 threads**, catalog **and** panel per iteration | **1** request at a time |
| rows | 94 real rows | 94 synthetic rows |

`read_bench.py`'s own docstring is "Read-storm benchmark: concurrent
catalog + panel reads"; `--threads` defaults to 6 and `--iterations` to 10.
Every T2/O4 number in the 2026-08-26 audit is therefore a
**concurrent-storm percentile**, never a sequential one.

Compared like-for-like, the multiplier disappears:

| measurement | 94 rows |
|---|---|
| offline bench, 10-concurrent, host (18 core) | 1.06 s |
| offline bench, 10-concurrent, container (2 CPU) | 1.96 s |
| audit, 6-thread storm, real rows, catalog+panel | 3.3 s |

That residual ~1.7× is panel reads interleaved into the same storm plus
whatever real rows cost over synthetic ones (§8 bounds the latter) — not a
hundredfold anything. **This is the finding that removes the *need* for
the virtiofs hypothesis** — §8 goes on to size virtiofs directly and finds
a real but small effect — and it explains why the hypothesis looked
necessary in the first place: a 118× gap demands an exotic cause; a 1.7×
gap does not.

## 2. Container filesystem A/B — not the driver, and partly impossible to run

Same bench, same container, same 2-CPU cgroup; only `TMPDIR` (and so the
synthetic fleet's filesystem) varies.

| arm | filesystem | 144 rows, catalog seq p50 | 144 rows, catalog 10-concurrent p50 |
|---|---|---|---|
| virtiofs (`/app/cache`) | virtiofs bind mount | **cannot run** | **cannot run** |
| overlay (`/tmp`) | overlayfs, VM-local | 36.4 ms | 3027 ms |
| tmpfs (`/dev/shm`) | tmpfs (pure RAM) | 36.5 ms | 3007 ms |
| host baseline (PR #1907) | APFS, 18 cores | 44.1 ms | 1600 ms |

Two results:

1. **The virtiofs arm is structurally impossible, not merely slow.** The
   clerk refuses to open a WAL database on it —
   `UnsupportedWalFilesystem: SQLite WAL authority requires a VM-local
   filesystem; ... resolves to 'virtiofs'`
   (`repository_lifecycle.assert_wal_filesystem_supported`). In production
   the clerk DB lives on the container-local **named volume**
   `learn-ai-alpaca-clerk-data`, mounted at `/app/artifacts/alpaca_clerk`.
   The authority the read path queries was never on virtiofs, so virtiofs
   could not have been amplifying its syscalls.
2. **Filesystem cost is not the driver.** tmpfs is RAM; overlayfs is
   VM-local disk. They are **identical to within 1 %** on both surfaces (0.1 %
   sequential, 0.7 % concurrent). Had per-syscall cost been driving
   the curve, tmpfs would have collapsed it.

**One caveat this A/B cannot see, measured separately in §8.** The bench
puts the *whole* synthetic fleet — DB and per-row lifecycle files — under
one `TMPDIR`. Production splits them: the clerk DB must be on the named
volume, but the per-row lifecycle files live under `artifacts/live_state`,
which **is** the virtiofs bind mount. So the arms above vary the filesystem
of a fleet whose file half production would place on virtiofs, and they
understate it. §8 measures that half directly: virtiofs is a real **5×** on
per-row file work — worth ~10 % of a read, not a hundredfold anything.

Also settled, from the same runs: **the container is not the problem
either.** It is marginally *faster* than the 18-core host sequentially
(36.4 ms vs 44.1 ms at 144 rows) and ~1.9× slower under 10-concurrency —
exactly what a 2-CPU cgroup predicts for a CPU convoy, and nothing more.
This closes #1907 §5's "re-run the bench in-container" item.

## 3. F13's 10-concurrent case, live

Ten concurrent catalog GETs against the deployed data plane, 52 rows,
50 bots actively trading:

| | per-request p50 | wall/round | expected if parallel |
|---|---|---|---|
| live, 52 rows, 50 trading | **7 682 ms** | 7 684 ms | 10 × 161 ms = 1 610 ms |

Every request takes essentially the **entire round**, and total work
inflates **4.8×** (offline it was 3.8×). This is full serialization on the
deployed topology, with the mechanism the offline profile named: an
event-loop-blocking roster slice plus GIL-holding `to_thread` projection
slices, now with a third tenant — 50 bot decision loops in the same
process.

Sequential reads at the same instant were 158–182 ms. **The gap between
161 ms alone and 7 682 ms at ten-concurrent is the whole finding**, and it
is why an operator sees timeouts while a `curl` looks healthy.

## 4. History depth — ruled out

A standing hypothesis this session was that real rows are expensive because
they carry accumulated clerk history, which the synthetic bench lacks
(`_build_fleet` seeds zero orders, fills, receipts, or custody
transitions). Measured at **fixed 52 rows** over ~12 minutes of live
trading:

| tick | custody transitions | decision receipts | orders | catalog seq p50 |
|---|---|---|---|---|
| t00 | 1 265 | 1 256 | 169 | 161 ms |
| t01 | 1 265 | 1 358 | 169 | 158 ms |
| t02 | 1 784 | 1 461 | 220 | 168 ms |
| t03 | 1 784 | 1 563 | 220 | 170 ms |
| t04 | 2 034 | 1 716 | 270 | 182 ms |
| t05 | 2 134 | 1 818 | 270 | 123 ms |

History grew **+69 %** (custody transitions) and orders **+60 %**; latency
stayed flat inside its noise band. **Ruled out.**

The supporting structural reason: the queries `_policy_context` runs per
bot hit small, indexed tables — `reconciliations` had 60 rows,
`uncertainties` 7 — and `EXPLAIN QUERY PLAN` confirms index use
(`SEARCH r USING INDEX ix_reconciliations_effect_attempted_at`). The
per-row cost is CPU-bound Python and per-row path/file work, as the
offline profile found; it is not SQL, and it does not scale with account
history.

## 5. Why the operator sees this and a `curl` does not

`Frontend/src/app/services/poll-timeout.ts` sets
`POLL_REQUEST_TIMEOUT_MS = 15_000` against a **5 s** catalog interval, and
three independent call sites poll through it
(`broker-v2-panel.service.ts:137`, `brokers.service.ts:81` and `:271`).
Independent pollers issue overlapping reads, so the operator's browser
generates exactly the concurrent storm measured in §3 — while any single
hand-run request returns in ~160 ms. The timeout is not wrong; the read
serializes.

## 6. T2/O4 reproduced at the audit's shape

Staged to the audit's exact roster shape — 144 catalog rows, 50 actively
trading — and measured with the audit's own methodology (`read_bench.py`'s
6 threads × 10 iterations, catalog **and** panel per iteration):

| | audit 2026-08-26 | this session |
|---|---|---|
| catalog p50 | 16.8 s | **8.78 s** |
| catalog p95 | 20.6 s | **10.58 s** |
| panel p50 | 2.7 s | **1.75 s** |
| panel p95 | — | 2.30 s |
| errors | — | 0 |

**T2/O4 reproduces.** The remaining ~2× is fleet composition: the audit ran
4 symbols × 6 strategies with 94 *real* legacy rows, this session ran a
homogeneous SPY `deployment_validation` fleet whose 92 ballast rows were
minted minutes earlier. The phenomenon, its shape, and its order of
magnitude are the same, and the audit's figure sits above
`POLL_REQUEST_TIMEOUT_MS` where this one sits just under it — which is
exactly the difference between "the operator sees a failed refresh" and
"the operator sees a slow one".

Sequential catalog reads at the same 144 rows and the same instant:
**421 ms** (52 rows: 161 ms) — linear in rows at ~2.9 ms/row, versus
~0.25 ms/row for the synthetic bench. §7 attributes that gap.

## 7. The discriminator: what each tenant actually costs

Same 144 rows, same instant, the only variable being whether the 50 bots
are running. The mass stop was clean — 50/50 `200`, median 586 ms, and
**zero non-flat positions before or after** (T3's lockstep stranding did
not bite: the whole cohort happened to be flat between cycles).

| 144 rows | 50 trading | 0 running | ratio |
|---|---|---|---|
| catalog sequential p50 | 421 ms | 267 ms | 1.6× |
| catalog storm p50 (6 threads) | 8.78 s | 2.58 s | **3.4×** |
| catalog storm p95 | 10.58 s | 3.11 s | 3.4× |
| panel storm p50 | 1.75 s | 0.77 s | 2.3× |
| storm wall | 99.1 s | 32.9 s | 3.0× |

**The asymmetry is the finding.** Fifty running bots cost a *lone* read
only 1.6×, but they cost a *concurrent* read 3.4×. Running bots do not
make the read do more work — they add GIL-competing tenants to a convoy
that is already serialized, so their cost is multiplied by the concurrency
rather than added to it. This is why the surface degrades so much faster
than "50 more threads" intuition predicts, and why the audit's idle→loaded
jump (3.3 s → 16.8 s) is steeper than its row growth (94 → 144).

Decomposing the live 144-row storm p50 of 8.78 s:

| factor | contribution |
|---|---|
| per-row work on real rows (sequential floor) | 267 ms |
| × concurrency convoy (6 threads, §3's mechanism) | ~9.7× → 2.58 s |
| × 50 running bots as additional GIL tenants | ~3.4× → 8.78 s |

Note the sequential floor itself: **267 ms for 144 real rows (1.85 ms/row)
against 36 ms for 144 synthetic rows (0.25 ms/row)** with zero bots running
in both cases. Real rows cost ~7.4× synthetic ones, and §4 has already
ruled out clerk history depth as the reason. §8 isolates what remains.

## 8. What a real row costs that a synthetic one does not

§7 left a 7.4× gap: 267 ms for 144 real rows vs 36 ms for 144 synthetic
rows, both with zero bots running. Two contributors, measured.

**Virtiofs, on the per-row file half only.** The same production code
(`stable_bot_lifecycle_state_path` → `strategy_instance_artifact_dir` →
path confinement → `BotLifecycleStateRepo.read`) over the same 142 real bot
directories, once on the virtiofs bind mount and once on a copy in the
container-local overlay:

| filesystem | full-roster pass p50 | per row |
|---|---|---|
| virtiofs (`/app/artifacts`) | 33.2 ms | 0.234 ms |
| overlay (`/tmp`) | 6.6 ms | 0.047 ms |

**5.0×, and worth ~27 ms of a 267 ms read (~10 %).** So the virtiofs
hypothesis was not wrong so much as badly mis-sized: it is a real effect on
a real part of the read, and it is nowhere near large enough to have been
the ~100× it was recruited to explain. It also only bites where the bench
could not see it, which is why #1907 could neither confirm nor size it.

**The remaining ~200 ms is real-row projection work.** A deployed bot
carries `runs`, `orders`, `positions`, `effect_operations` rows and roughly
ten artifact files (`sealed_program_v2.json`, `strategy_instance.json`,
`carryover_checkpoint.json`, `program_build_evidence/`, `run_outcomes/`,
`run_replay_receipts/`, …) where a bench row carries a run and two JSON
files. `bench_panel_read_latency.py`'s own docstring already flags its
stopped-panel figures as "a floor" because its `preview_resume_admission`
stand-in skips the runner's admission-policy fact stack. **This session
sizes that floor: the bench understates a real roster by ~7×**, which is
worth recording in the bench itself so no future reader treats its absolute
numbers as production ones.

## 9. O2 — deploy latency under fleet load

Deploy latency was measured twice. The **first burst** (49 deploys, 2 → 52
rows, no ballast) showed essentially no curve: median 1 340 ms for deploys
1–10 rising only to 1 520 ms for 41–49, max 2 816 ms. On that evidence O2
did not reproduce.

The **ballast burst** did, because it ran where the audit's did: while 50
bots traded and the roster climbed from 52 to 144 rows. Reconstructed from
`strategy_instances.created_at_ms` (each interval is one deploy plus one
stop):

| roster rows | n | median | max |
|---|---|---|---|
| ~53–75 | 22 | 589 ms | 698 ms |
| ~75–98 | 23 | 700 ms | 3 822 ms |
| ~98–121 | 23 | **3 701 ms** | 4 210 ms |
| ~121–144 | 23 | **3 691 ms** | 4 338 ms |

**A 6.3× rise with row count, plateauing near 3.7 s.** Milder than the
audit's 0.4 s → ~15 s, but the same curve, and it settles O2's attribution:
deploy cost rises with **roster rows**, not with the number of concurrent
deploys or with elapsed session time. That is the same O(rows) projection
work §7 measured on the read path.

**O2 and T2/O4 do share a root, as the audit suspected — but it is not a
per-account lock.** It is that admission and read both pay O(rows)
projection cost, and both are then multiplied by whatever else holds the
GIL. Nothing in either profile waits on a lock.

## 10. S12d — does not reproduce

After the mass stop, at 144 rows with **zero** running bots, sampled over
three minutes:

| t+ | 20 s | 40 s | 60 s | 80 s | 100 s | 120 s | 140 s | 160 s | 180 s |
|---|---|---|---|---|---|---|---|---|---|
| CPU | 23.8 % | 24.7 % | 24.6 % | 24.6 % | 24.6 % | 25.0 % | 24.9 % | 24.6 % | 24.4 % |

**24–25 % of one core, flat, with no upward drift** — against S12d's
reported **77 %**. The bullet's second symptom does not reproduce either:
it reports 105–145 s panel reads after mass stop; measured panel storm p50
was **0.77 s** and sequential catalog p50 **267 ms**, unchanged at
**270 ms** after a further three minutes of settling. No data-plane restart
was needed at any point.

The residual 24–25 % is consistent with the known periodic loops —
the reconciliation sweep (15 s), the Alpaca market-clock poll (1 s), the
lease heartbeat, stream-health sync (15 s) — over a 144-row roster, and is
not itself pathological.

**This is a non-reproduction, not a fix**, and the conditions differ in one
way that matters: the 2026-08-26 run had IB Gateway live on port 4002
(§1 of that audit), and IBKR has since been decommissioned (#1813). The
IBKR auto-reconnect monitor polls at 3 s and is gated on
`ibkr_settings.broker_enabled`. That is a plausible but **unproven**
explanation for the original 77 %; this session did not attribute the
original observation, only established that it does not occur today.

## 11. What this decides

#1801's rule was *profile first, then choose between reducing per-read
work, changing lock granularity, or adjusting the poll budget.* On the
combined offline + live evidence:

**Lock granularity — not indicated, now on both paths.** The offline
profile showed read serialization reproducing with zero writers and zero
admission work. This session shows the deploy curve (§9) tracking *roster
rows*, not concurrent deploys or session age. Nothing in either profile
waits on a lock. The audit's suspected per-account contention is
**disfavoured for O2 as well as for T2/O4** — they do share a root, but it
is O(rows) projection work, not a lock.

**Poll budget — leave `POLL_REQUEST_TIMEOUT_MS` alone**, per the issue.
The timeout is not wrong; the read is slow. But §5 surfaces a *separate*
client-side fact that is not timeout tuning: three independent poll sites
drive overlapping reads on a 5 s interval against reads measured here at
2.6–8.8 s. Coalescing that fan-out is its own change on its own surface and
should be its own issue — it reduces the concurrency the server sees, which
is the term everything else is multiplied by.

**Reducing per-read work — the lever, and the live numbers reorder it.**
In descending measured leverage:

1. **Move the roster-building slice off the event loop** into the
   `to_thread` cut that already reads the projections (#1907 §5(d)). The
   convoy multiplier measured live is **~9.7×** (267 ms → 2.58 s at 6
   threads) and *every other saving is multiplied by it*. This is now
   clearly first, where #1907 listed it last.
2. **Compute `build_recovery_catalog` once per bot per read, not twice**
   (#1907 §5(a)). Still the dominant per-row row; confirmed still 2× in
   current code (`projections.py:255` inside the `to_thread` cut, and
   `sqlite_panel_source.py:884→908` on the loop thread).
3. **Resolve the artifacts root once per read, not per row** (#1907
   §5(b)), and **batch the lifecycle reads** (#1907 §5(c)). §8 raises the value
   of both: this work runs on virtiofs in production at **5×** the cost the
   host bench implied.
4. **Structural, and not proposed here:** 50 running bots multiply a
   concurrent read by **3.4×** (§7) purely as GIL tenants. Separating bot
   decision loops from the read path is the only change that addresses
   that term, and it is far larger than anything above.

None of the above is a cache: each read still derives everything from the
same durable inputs at the same instant, so #1776's single-reconciler
invariant is untouched.

**Also decided: nothing here justifies moving the artifacts tree off
virtiofs.** It is a real 5× on ~10 % of a read (§8). Fix the O(rows) work
first and re-measure; the mount is not the story.

## 12. Corrections to the offline profile (#1907)

Recorded explicitly because a merged document says otherwise:

| #1907 said | this session measured |
|---|---|
| "The live idle 3.3 s (94 rows) is ~118× this host's 28 ms" | Not comparable — 3.3 s is a **6-thread storm** percentile, 28 ms is **sequential**. Like-for-like the gap is ~1.7× (§1). |
| virtiofs "**consistent** with the ~100× idle multiplier; needs the §5 container A/B to be pinned" | Pinned: **5×** on per-row file work, ~10 % of a read (§8). The clerk DB was never on virtiofs — the WAL guard forbids it (§2). |
| §5 lever order: (a) recovery catalog, (b) path confinement, (c) lifecycle batching, (d) move the roster slice off the loop | **(d) is first** — it attacks the ~9.7× convoy that multiplies (a)–(c) (§11). |
| "may still govern O2 (deploy latency), which is write-path and live-only" | O2 measured: tracks roster rows, 6.3× (§9). Same O(rows) root, still no lock. |

The offline profile's *mechanism* — per-row work split across an
event-loop-blocking slice and GIL-holding `to_thread` workers — is
confirmed live and unchanged. What was wrong was the sizing of the live gap
and the causal weight given to the filesystem, both traceable to the single
sequential-vs-concurrent comparison error in its §3.

## 13. Incidental: roster rows are unbounded and read cost is linear in them

Cleanup after this session could not complete through the product's own
affordances. All 142 profiling bots stopped cleanly (50/50 `200`, zero
non-flat positions), but **`retire` is disabled on every one of them**:

> `STRATEGY_STILL_RUNNABLE` — "Retire clears a registration that is
> provably dead: its strategy program is gone from the runtime, or the
> broker has durably answered that its symbol is not a listed asset.
> Neither proof holds for this bot."

That is #1795's contract working as designed — Retire is for permanently
*inadmissible* bots, and a stopped-but-healthy SPY bot is not one. The
consequence is worth stating next to this profile: **a healthy bot that is
finished with can be stopped but not removed, so catalog rows only ever
accumulate**, and §6/§9 measure read *and* deploy cost as linear in exactly
that number (~2.9 ms/row sequential live; deploy 6.3× from 53 → 144 rows).
The 2026-08-26 audit hit the same wall from the other side, describing its
94 leftover rows as "legacy roster rows retained deliberately as read-scale
ballast."

The only sanctioned removal path found is the paper-only developer
clean-slate reset (`scripts/manage_alpaca_sqlite_clerk.py dev-reset`),
which moves the **whole account's** authority aside rather than retiring
individual bots.

This is not a defect in either #1795 or #1801, and no change is proposed
here — but "roster rows grow monotonically and every read pays for them" is
a cost-curve fact that belongs with the rest of this profile, and it is
plausibly why the audit's baseline was 94 rows before a single bot was
deployed.
