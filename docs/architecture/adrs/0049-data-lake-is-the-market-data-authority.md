# ADR 0049: The data lake is the authority for historical bar data

**Status:** Accepted 2026-08-27

**Vocabulary:** `CONTEXT.md` § "Data lake" — **Data lake**, **Lake catalog**, **Lake artifact**, **Data coverage**, **Claim / lease**. Owed on acceptance: `CONTEXT.md` had no entry for any of the five, and the observatory surface ([#1838](https://github.com/tim1016/learn-ai/issues/1838)) puts coverage and artifacts in front of an operator. Per ADR 0040 Decision 4.

- **Date:** 2026-08-27
- **Provenance:** PRD [#1825](https://github.com/tim1016/learn-ai/issues/1825),
  decision ticket [#1831](https://github.com/tim1016/learn-ai/issues/1831).
  Design spec: `2026-05-20-polygon-lean-data-lake-design.md`, pruned from the
  tree on 2026-07-04 and recoverable from git history (see References).
- **Related:** ADR 0001 (control-plane substrate), ADR 0022 (temporal authority).

## Context

Historical bar data has no single owner. The policy-keyed cache under
`app/engine/data/policy_store.py` is the closest thing to one, and it is a cache
in the literal sense: it knows what files it happens to hold, not what data the
system is supposed to own. It has no catalog, so "what do we have?" is answered
by walking the filesystem; no coordination, so two runs wanting the same symbol
serialize on an advisory lock and nothing else; and no record of what a day's
bytes *are*, so nothing can tell a corrupted zip from a good one.

The costs are not hypothetical. Issue [#1830](https://github.com/tim1016/learn-ai/issues/1830)
found one symbol's provenance carrying 43 fetches of the same two years, because
the store's completeness test counts exchange holidays as days it should have,
so a window containing one can never be complete and re-fetched forever. That
fix bounded the refetch to the missing day; it could not close it, because
closing it needs a holiday-aware notion of which days are *expected* — which is
a catalog's job, not a cache's.

The 2026-05-20 design spec proposed a data lake: immutable LEAN-format files as
the canonical bytes, with Postgres as a catalog over them. This ADR records the
decision to adopt it as the authority, and the two places where what we are
building deviates from what that spec described.

## Decision

**The data lake is the single authority for historical bar data.** Immutable
LEAN-format files are canonical; Postgres is the catalog and coordination plane
over them. Engines and charts read through the lake; the policy store is
imported into it and then retired ([#1832](https://github.com/tim1016/learn-ai/issues/1832),
[#1840](https://github.com/tim1016/learn-ai/issues/1840)).

### 1. This does not contradict ADR 0001, and here is why

ADR 0001 held the live-runtime control plane on JSON + Parquet + hash sidecars
and said Postgres may only ever be a *projection* — "never the source of truth",
never owning desired state, the run ledger, or the audit trail.

The lake honors that doctrine rather than carving an exception from it:

- **The bytes stay files.** A bar day is a LEAN zip on disk, hashed. Losing
  Postgres loses the index, not the data.
- **Postgres owns no market data.** It owns *statements about* market data —
  which artifact exists, its hash, its provenance, and who currently holds a
  claim to fetch it.

Rebuildability is the test that makes "projection" mean something, so it is
worth being exact about what survives a lost catalog rather than waving at it.
Three kinds of row content, three different answers:

- **Identity, hash, and coverage are rebuildable** by walking the lake and
  re-hashing. This is the index, and it is the bulk of the catalog.
- **Lease state is deliberately *not* rebuilt.** A lease is a claim on work in
  flight; one that survived a catalog rebuild would be a stale claim blocking a
  fetch nothing is performing. Losing it is correct, not a gap.
- **Provenance and fetch history are not derivable from bytes** — when a day was
  fetched, under which provider parameters, after how many failed attempts, and
  (after the import in [#1832](https://github.com/tim1016/learn-ai/issues/1832))
  whether it was fetched from the provider at all or imported from the policy
  store. An imported zip and a fetched zip are byte-identical.

That last category is the one ADR 0001 speaks to directly, because provenance is
audit trail, and ADR 0001 says Postgres never owns the audit trail. **So it does
not: provenance keeps a file-side home in the lake, and the catalog's copy is
the projection of it.** This is not a concession invented here to rescue the
argument — PRD #1825 already requires the existing cache's provenance documents
to be carried into the import's provenance trail rather than deleted, because
they are the evidence for the [#1830](https://github.com/tim1016/learn-ai/issues/1830)
refetch leak. Writing provenance file-side keeps that evidence on the substrate
ADR 0001 designated for it, and makes the rebuildability claim above true rather
than approximately true.
- **The scope is disjoint.** ADR 0001 governs the live-runtime control plane:
  run identity, decisions, executions, trades, kill switches. None of that moves.
  Historical bar data was never in that substrate; it sat in an uncatalogued
  cache beside it.

The one genuinely new thing is **coordination**: claim/lease rows that let two
concurrent ensures for the same artifact produce one fetch. ADR 0001's third
projection-layer trigger anticipated exactly this shape — a store the file
substrate "can't cleanly express". A filesystem advisory lock is what the policy
store already used and is what a lease replaces; this is that trigger firing,
not a doctrine change.

### 2. Orchestration is in-process Python, not Backend-driven

The 2026-05-20 spec put .NET in charge: Backend records the run request, calls
Python `ensure_data`, evaluates a partial-coverage policy table, calls
`prepare_run`, then launches the engine (spec §§ 61–62, 103–122). Python was to
have "no top-level orchestration".

**We are not building that.** Run materialization calls the lake's `ensure_data`
in-process, at the same point in the Python path where the policy store's export
sits today. The Backend's run lifecycle is unchanged.

The deviation is deliberate. The spec's flow was designed when the Backend was
expected to own the run lifecycle end-to-end; it does not, and adopting the
HTTP-orchestrated version would mean building a cross-process protocol,
a partial-coverage policy table in .NET, and an idempotency layer keyed on
`request_id` — to reach a materialization step that already runs correctly in
one process. It also spreads decisions about LEAN-format semantics across two
languages, which the spec itself argued against in the same section that
assigned orchestration to .NET (§ 122: "LEAN-format knowledge ... stays
Python-side").

**What the deviation gives up.** The spec's flow carried two capabilities that
in-process orchestration does not inherit for free, and a deviation record that
lists only its own reasons is an argument, not a record:

- **The partial-coverage policy table** (§ 528): a `run_type` × `failure.reason`
  matrix in Backend deciding whether a run proceeds on incomplete data. In-process
  orchestration still has to answer that question; it just answers it in Python,
  and nothing in this decision says how. That is a real open question for
  [#1833](https://github.com/tim1016/learn-ai/issues/1833), not a solved one.
- **Backend-side `request_id` idempotency** (§§ 541–543), which deduped retried
  orchestration calls. Materializing in-process removes the retrying caller, so
  the need mostly dissolves — but "mostly" is doing work in that sentence, and
  the catalog's own claim/complete semantics are what has to carry it.

This is recorded rather than silently dropped because the spec is cited by
section number throughout the lake's source, and a reader who recovers it will
otherwise find §§ 103–122 describing a flow that does not exist.

### 3. The provider-licensing gate is open, and is recorded as open

Building a durable archive of vendor market data raises a question this decision
does not answer: **whether the provider's terms permit retaining that data for
the archive's lifetime.** Polygon's retention and redistribution terms have not
been reviewed against what the lake does.

This gate is **recorded, not assumed passed.** Nothing in this ADR should be read
as a finding that retention is permitted. Clearing it is a human task and a
prerequisite to treating the lake as a long-lived archive rather than a working
cache with a catalog. Adopting the lake for run materialization does not depend
on it — that use retains no more than the policy store already does — but the
archive framing does.

The 2026-05-20 spec did not raise this. It is added here because the lake
changes the posture from "cache we happen to keep" to "data we own", and the
second is the one that needs a licence.

## Consequences

**Positive:**
- One answer to "what data do we own?", from the catalog rather than a
  filesystem walk — which is what makes the observatory page
  ([#1838](https://github.com/tim1016/learn-ai/issues/1838)) possible at all.
- Per-artifact hashes make a corrupt zip detectable instead of silently readable.
- Claim/lease coordination replaces "two runs serialize and both fetch".
- A holiday-aware expected-day set becomes expressible, which is the actual cure
  for the [#1830](https://github.com/tim1016/learn-ai/issues/1830) refetch leak.
- The read seam does not move: engines and the bars endpoint keep consuming
  through the existing LEAN readers, with root resolution re-pointed at the lake.

**Negative:**
- Postgres becomes a *runtime* dependency of run materialization, where the
  policy store needed only a filesystem. Bounded by the rebuildability above: a
  lost catalog is re-derivable, not a data loss.
- Two sources of bar data coexist during rollout, gated by the data-lake flag.
  The flag is removed in the retirement slice; until then, "which path served
  this run?" is a real question, which is why the flag state belongs in the
  run's evidence.
- The recovered spec disagrees with the built system on orchestration. Mitigated
  by § 2 above, and only by it — the spec is not in the tree to be corrected.

## References

- ADR 0001 — control-plane substrate; the files-first doctrine this is scoped against.
- ADR 0022 — temporal authority; the calendar module that supplies trading days.
- PRD [#1825](https://github.com/tim1016/learn-ai/issues/1825) — the enablement plan and its slices.
- Issue [#1830](https://github.com/tim1016/learn-ai/issues/1830) — the refetch leak this decision's catalog is the cure for.
- The pruned design spec, cited by section number in the lake's source, is recoverable with:

  ```bash
  git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
  ```
