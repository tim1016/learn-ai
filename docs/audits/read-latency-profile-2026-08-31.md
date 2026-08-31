# Panel/Catalog Read-Latency Profile — 2026-08-31 (#1801)

**Scope.** The profile-first deliverable #1801 asks for, produced offline with
a reproducible bench so the read-side cost curve can be re-measured after any
change without staging a 50-bot fleet. Source finding: T2/O4 in
`docs/audits/bot-fleet-stress-2026-08-26.md` (catalog p50 16.8 s under fleet
load; 3.3 s idle at 94 rows) and F13 in `docs/known-gaps.md` §9 (panel reads
serialize under concurrency; its 10-concurrent condition is carried into
#1801 and remeasured here).

**Tool.** `PythonDataService/scripts/bench_panel_read_latency.py` — real
`ClerkSqliteRepository` + real `SqliteAlpacaClerkFacade` + the real
`broker_v2_panel` router over ASGI. The only stand-ins are the broker port
(which doubles as the #1776 purity fence: the bench refuses runs whose reads
contacted it — every run below reports `broker calls during bench: 0`) and
the bot task registry's liveness answer (a dict lookup in production too).
Stopped bots' panel reads route through the **real**
`default_start_custody_projection` seam.

Reproduce:

```bash
cd PythonDataService
DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m scripts.bench_panel_read_latency \
    --rows 94 144 --requests 20 --rounds 5 --profile
```

## 1. Measurements (host: Apple M5 Max, 18 cores, local APFS, host venv)

### 94 rows (the audit's idle shape)

| surface | n | p50 ms | p95 ms | max ms | wall/round ms |
|---|---|---|---|---|---|
| catalog GET (sequential) | 20 | 28.1 | 29.5 | 30.3 | — |
| panel GET (sequential) | 20 | 5.4 | 5.7 | 6.5 | — |
| panel GET (10-concurrent) | 50 | 111.6 | 133.7 | 139.7 | 135.7 |
| catalog GET (10-concurrent) | 50 | 1050.0 | 1092.9 | 1106.2 | 1088.0 |

### 144 rows (the audit's loaded shape)

| surface | n | p50 ms | p95 ms | max ms | wall/round ms |
|---|---|---|---|---|---|
| catalog GET (sequential) | 20 | 42.7 | 43.4 | 43.5 | — |
| panel GET (sequential) | 20 | 5.4 | 6.2 | 6.3 | — |
| panel GET (10-concurrent) | 50 | 123.6 | 136.7 | 142.4 | 140.7 |
| catalog GET (10-concurrent) | 50 | 1595.4 | 1661.3 | 1690.8 | 1661.1 |

`--disable-gc` at 144 rows: catalog 10-concurrent wall 1628 ms — unchanged.
**GC pressure is ruled out** as the concurrent-inflation cause.

## 2. What the profile says (cProfile, catalog reads, 144 rows)

Per catalog read, ~96 % of the time is the per-bot projection loop
(`SqliteClerkProjectionReader.bot_snapshot`, 144 calls/read). Inside one
read's ~42 ms:

| component | ms/read | notes |
|---|---|---|
| `build_recovery_catalog` (recovery_policy.py:763) | ~24 | called **2× per bot per read** (2 880 calls / 10 reads); `_decision` runs 2 304×/read, `_token` 2 304×/read |
| `lifecycle_record` (sqlite_roster_status.py:79) | ~22 | of which **~16 ms is path construction** — `strategy_instance_artifact_dir` → `confine_path_to_root` (`Path.resolve()` syscalls per row); the actual file `read()` is ~4.5 ms |
| economic rollups (one-revision S2 read) | ~3 | already batched; not the problem |
| calendar session window | ~3 | once per read, cached-ish |

Two structural facts matter more than any single row above:

1. **The whole per-row loop runs inside `asyncio.to_thread`**
   (`sqlite_panel_source.read_all` → `to_thread`), and the work is
   CPU-bound Python holding the GIL.
2. **Per-row cost is linear in rows** (28 ms @ 94 → 43 ms @ 144, ~0.3 ms/row
   on this host) and syscall-heavy (path resolution + lifecycle file read
   per row).

## 3. The causal chain, and how it maps to the live numbers

**F13's 10-concurrent case, remeasured (offline): confirmed, with a
mechanism.** Ten concurrent catalog reads take ~1.6 s *each* — wall/round
equals the slowest request, and every request's latency ≈ the full round.
That is full serialization: ten `to_thread` workers each need the GIL for
~40 ms of pure-Python work, so they convoy. Worse, total work inflates
~3.8× (10 × 43 ms = 430 ms expected; 1 661 ms measured) — thread-switch and
cache-eviction overhead between GIL-contending workers, not GC (§1). Panel
GETs show the same shape at smaller magnitude (5.4 ms alone → 124 ms each at
10 concurrent).

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
| GIL convoy: CPU-bound per-row projection in `to_thread` × concurrent polls | **Confirmed offline** as the serialization + inflation mechanism. |
| Filesystem syscall amplification (virtiofs bind mount) on per-row path-confinement + lifecycle reads | **Consistent** with the ~100× idle multiplier; needs the §5 container A/B to be pinned. |

## 5. What this decides, and what stays owed

The issue's rule was *profile first, only then choose between reducing
per-read work, changing lock granularity, or adjusting the poll budget*.
On this evidence:

- **Lock-granularity work on the read path is not indicated.** Nothing in
  the profile waits on a lock.
- **Reducing per-read work is the lever, in this order:**
  (a) `build_recovery_catalog` — recompute once per bot per read instead of
  twice, or hoist the per-row invariant parts out of the loop (~60 % of
  roster-status time); (b) path-confinement — resolve the artifacts root
  once per read, not per row (~16 ms/read of `Path.resolve()`);
  (c) lifecycle records — one batched directory pass instead of 144
  independent open/read cycles. None of these is a cache: each read still
  derives everything from the same durable inputs at the same instant, so
  #1776's single-reconciler invariant is untouched.
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
