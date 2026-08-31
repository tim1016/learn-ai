# Panel/Catalog Read-Latency Profile — 2026-08-31 (#1801)

> **Partially corrected 2026-08-31 by the live half**, in
> `read-latency-profile-live-2026-08-31.md`. The **mechanism** below —
> per-row work split across an event-loop-blocking slice and GIL-holding
> `to_thread` workers — is confirmed live and stands. Three things here do
> not, all traceable to one comparison error in §3: the live audit figures
> it compares against (3.3 s / 16.8 s) are **6-thread concurrent-storm**
> percentiles from `scripts/dev/fleet/read_bench.py`, not sequential ones,
> so §3's "~118×" is unlike-for-unlike (like-for-like it is ~1.7×); §4's
> virtiofs row is **pinned at 5× on ~10 % of a read**, not "consistent with
> a ~100× multiplier", and the clerk DB was never on virtiofs at all
> because the WAL guard rejects it; and §5's lever ordering is reversed —
> (d), moving the roster slice off the event loop, is first, because it
> attacks the ~9.7× convoy that multiplies (a)–(c). See that document's §12.


**Scope.** The profile-first deliverable #1801 asks for, produced offline with
a reproducible bench so the read-side cost curve can be re-measured after any
change without staging a 50-bot fleet. Source finding: T2/O4 in
`docs/audits/bot-fleet-stress-2026-08-26.md` (catalog p50 16.8 s under fleet
load; 3.3 s idle at 94 rows) and F13 in `docs/known-gaps.md` §9 (panel reads
serialize under concurrency; its 10-concurrent condition is carried into
#1801 and remeasured here).

**Tool.** `PythonDataService/scripts/bench_panel_read_latency.py` — real
`ClerkSqliteRepository` + real `SqliteAlpacaClerkFacade` + the real
`broker_v2_panel` router over ASGI. The stand-ins are the broker port
(which doubles as the #1776 purity fence: the bench *aborts* any run whose
measured reads contacted it — every run below reports `broker calls during
bench: 0`) and the bot task registry. The registry's liveness answer is a
dict lookup in production too; its `preview_resume_admission` stand-in runs
the **real** `default_start_custody_projection` seam but deliberately skips
the runner's admission-policy fact stack (process/runtime/checkpoint/
validation/market facts need a full `BotTaskRegistry` with real bindings),
so stopped-panel figures are a floor for that surface and are reported
separately from running-panel figures.

Reproduce (this exact invocation produced every table and the profile below):

```bash
cd PythonDataService
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.bench_panel_read_latency \
    --rows 94 144 --requests 20 --rounds 5 --profile --profile-limit 18
```

## 1. Measurements (host: Apple M5 Max, 18 cores, local APFS, host venv)

### 94 rows (the audit's idle shape)

| surface | n | p50 ms | p95 ms | max ms | wall/round ms |
|---|---|---|---|---|---|
| catalog GET (sequential) | 20 | 30.0 | 33.4 | 35.8 | — |
| panel GET (sequential, stopped bots) | 20 | 5.6 | 6.1 | 6.8 | — |
| panel GET (sequential, running bots) | 20 | 5.3 | 5.5 | 5.5 | — |
| panel GET (10-concurrent, interleaved states) | 50 | 111.1 | 143.4 | 146.6 | 126.5 |
| catalog GET (10-concurrent) | 50 | 1058.9 | 1109.0 | 1116.1 | 1088.4 |

### 144 rows (the audit's loaded shape)

| surface | n | p50 ms | p95 ms | max ms | wall/round ms |
|---|---|---|---|---|---|
| catalog GET (sequential) | 20 | 44.1 | 46.7 | 50.6 | — |
| panel GET (sequential, stopped bots) | 20 | 5.6 | 5.9 | 6.2 | — |
| panel GET (sequential, running bots) | 20 | 5.4 | 5.5 | 5.5 | — |
| panel GET (10-concurrent, interleaved states) | 50 | 119.4 | 145.5 | 149.1 | 146.3 |
| catalog GET (10-concurrent) | 50 | 1600.2 | 1670.5 | 1683.3 | 1670.8 |

Panel sampling is stratified by lifecycle state (stopped bots run the resume
custody projection; running bots do not) and the concurrent rounds interleave
both states across the whole fleet, so neither path can silently dominate the
sample. `--disable-gc` at 144 rows: catalog 10-concurrent wall 1628 ms —
unchanged. **GC pressure is ruled out** as the concurrent-inflation cause.

## 2. What the profile says (cProfile, catalog reads, 144 rows, `--requests 20`)

The profiled 20 reads make 2 880 `bot_snapshot` calls (144 × 20 — one per
row per read) and 5 760 `build_recovery_catalog` calls (**2× per bot per
read**, with `_decision`/`_token` at 46 080 calls each). One ~44 ms read
splits into **two per-row slices on two different threads**:

| slice | where it runs | ms/read (unprofiled ≈) | inside it |
|---|---|---|---|
| `read_all` → `bot_snapshot` ×144 (projection cut) | **worker thread** (`asyncio.to_thread`, `sqlite_panel_source.py:624`) | ~27 | SQLite row reads + one of the two `build_recovery_catalog` passes |
| `_bound_roster_statuses` ×144 + `build_sqlite_catalog` | **event-loop thread** (called synchronously after the awaits, `read_sqlite_catalog`) | ~16 | the second `build_recovery_catalog` pass, `lifecycle_record` per row — where ~⅓ is path construction (`strategy_instance_artifact_dir` → `confine_path_to_root`, `Path.resolve()` syscalls per row) around a ~µs-scale file `read()` |

(Economic rollups are already batched at one revision, ~3 ms; the calendar
session window is once per read.)

Two structural facts matter more than any single row above:

1. **The per-row work is CPU-bound Python split across the event loop and a
   `to_thread` worker** — the loop slice blocks the only event loop, and the
   worker slices hold the GIL.
2. **Per-row cost is linear in rows** (30 ms @ 94 → 44 ms @ 144, ~0.3 ms/row
   on this host) and syscall-heavy (path resolution + lifecycle file read
   per row).

## 3. The causal chain, and how it maps to the live numbers

**F13's 10-concurrent case, remeasured (offline): confirmed, with a
mechanism.** Ten concurrent catalog reads take ~1.6 s *each* — wall/round
equals the slowest request, and every request's latency ≈ the full round.
That is full serialization, produced by **both halves of §2's split**: each
request's event-loop slice (~18 ms of roster building per read) blocks the
single loop every other request needs, and the ten worker-thread slices
convoy on the GIL against each other and against the loop thread. Total work
also inflates ~3.8× (10 × 44 ms = 440 ms expected; 1 671 ms measured) —
thread-switch and cache-eviction overhead between GIL-contending threads,
not GC (§1). Panel GETs show the same shape at smaller magnitude (~5.5 ms
alone → ~119 ms each at 10 concurrent, both lifecycle states interleaved).

**The live idle 3.3 s (94 rows) is ~118× this host's 28 ms.** The per-row
work is dominated by filesystem syscalls (resolve chains + per-row file
reads) and GIL-bound Python; the production data plane runs in podman on
macOS where the artifacts tree is a **virtiofs bind mount** (per-syscall
cost ~ms, not ~µs) on a CPU-limited VM — and the frontend polls several
surfaces concurrently, so even "idle" catalog reads interleave with panel
and gallery polls in exactly the convoy measured above. Multiplier ≈
(virtiofs syscall cost × ~10 syscalls/row × rows) + convoy inflation is the
right order of magnitude; the container A/B in §5 pins it precisely.

**The live loaded 16.8 s adds the third GIL tenant**: 50 trading bots'
decision loops run in the same process and contend for the same GIL as the
`to_thread` read workers. No per-account *lock* is needed to produce any of
the observed shapes.

## 4. Hypothesis scorecard

| hypothesis | verdict on this evidence |
|---|---|
| Per-account lock contention between admission work and running bots' clerk operations (the audit's suspect, shared with O2) | **Disfavored for the read side** — the full serialization and inflation reproduce offline with zero writers and zero admission work. It may still govern O2 (deploy latency), which is write-path and live-only. |
| GC pressure | **Ruled out** (`--disable-gc` unchanged). |
| Single-process CPU convoy: per-row work split between an event-loop-blocking slice and GIL-holding `to_thread` workers, × concurrent polls | **Confirmed offline** as the serialization + inflation mechanism (both halves measured; see §2). |
| Filesystem syscall amplification (virtiofs bind mount) on per-row path-confinement + lifecycle reads | **Consistent** with the ~100× idle multiplier; needs the §5 container A/B to be pinned. |

## 5. What this decides, and what stays owed

The issue's rule was *profile first, only then choose between reducing
per-read work, changing lock granularity, or adjusting the poll budget*.
On this evidence:

- **Lock-granularity work on the read path is not indicated.** Nothing in
  the profile waits on a lock.
- **Reducing per-read work is the lever, in this order:**
  (a) `build_recovery_catalog` — recompute once per bot per read instead of
  twice, or hoist the per-row invariant parts out of the loop (the dominant
  row inside both slices); (b) path-confinement — resolve the artifacts root
  once per read, not per row (`Path.resolve()` per row); (c) lifecycle
  records — one batched directory pass instead of 144 independent open/read
  cycles; (d) move the roster-building slice off the event loop into the
  same `to_thread` cut that already reads the projections, so a slow catalog
  read stops blocking every concurrent request. None of these is a cache:
  each read still derives everything from the same durable inputs at the
  same instant, so #1776's single-reconciler invariant is untouched.
- **`POLL_REQUEST_TIMEOUT_MS` stays untouched**, per the issue: the timeout
  is not wrong; the read is slow.
- **Still owed, live (the bench's docstring carries the same list):** the
  container A/B for the virtiofs multiplier (same bench, artifacts on the
  bind mount vs a named volume vs tmpfs); O2 deploy latency under fleet
  load; the S12d post-mass-stop 77 % CPU residue; and re-running this bench
  in-container to convert §3's order-of-magnitude mapping into a measured
  one.

**F13 is remeasured but not closed.** Its bullet in `docs/known-gaps.md` §9
now points here; the deletion condition (a live 10-concurrent remeasure on
the deployed topology) still stands. The offline measurement satisfies the
mechanism question — reads serialize and the serialization is not load-dependent
— but the live magnitudes belong to the deployed environment.

No production code was changed by this profiling pass; implementation
choices from §5 belong to their own decision, per #1801's framing.
