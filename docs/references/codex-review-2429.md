# Published data snapshots versus bytes consumed by a run

Ticket: [#2429](https://github.com/tim1016/learn-ai/issues/2429). Baseline: `10b5f31b529c8507bd19bb24015f3d85fa9aba43`. Date: 2026-09-24. Research branch: `research/codex-2429-snapshot`.

This bounded review finds **two High issues**: existing exact-byte receipts are not consistently bound to the read, and filesystem-only admission can accept bytes from a publication whose catalog completion failed. Six synthetic cases exercise successful replacement, failed completion/commit, and a same-buffer positive control. They do not re-count factor coverage, map history bounds, adjustment vintage, or bar-content validity.

## Decision context and scope of the claims

The governing rationale is narrower than a universal immutable-snapshot requirement:

- [ADR 0049 §1b](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md#L43) says “integrity is catalog-verified, not filesystem-enforced.” Files are canonical, but publication replaces a fixed identity path; readers need evidence that the catalog's statement still describes the bytes they use.
- [Amendment A3](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md#L181) withdraws the requirement that every run have a durable exact-byte manifest as “overkill for this platform's needs,” with no replacement durable store planned. **This review does not reinstate that requirement**, count its absence as a defect, or recommend rebuilding the cancelled table. It examines receipts that the current compatibility and LEAN paths do produce and explicitly describe as input identity.
- The Python [cheap-stat rationale](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L353) says hashing every reused file “would turn a stat-cost check into an I/O-bound one.” That is a valid reason to keep a cheap availability gate. It does not make a separately advertised exact-byte receipt bind to a later read. More frequent stat checks, or an unbound hash just before/after the run, cannot provide that binding.
- The [zero-copy lake-mount rationale](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/lean_sidecar/lake_mount.py#L41) accepts undeclared-read access in exchange for eliminating per-run copying, while promising “reproducibility evidence for declared inputs.” R1 concerns replacement of a **declared** input by the authorized writer. A read-only consumer mount does not stop that writer. The recommendation preserves the single file authority and permits zero-copy/versioned views; it does not require wholesale per-run byte copies.

## Findings

### R1 — Compatibility and LEAN manifests can describe different bytes from the run's input

**Claim:** Python compatibility pinning hashes before an ordinary reader opens the file, and LEAN lake mode hashes after the launcher has finished. Neither path pins the input generation for the lifetime of the corresponding read. A valid, fully committed replacement can make the advertised receipt disagree with consumed bytes.

**Goal:** Correctness; architecture; intelligence.

**Severity:** **High.** Results can be attributed to the wrong input dataset, confounding engine parity and reproducibility. This is not evidence of a wrong live order or of a specific production loss.

**Evidence — reproduced, with scopes distinguished:**

1. **Actual Python backtest:** [`_pin_compatibility_fixture`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/engine_backtest_service.py#L376) describes its work as freezing the exact minute-zip bytes consumed by a compatibility pair. It records a [hash of paths at that moment](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/engine/data/policy_store.py#L88). The [ordinary reader is selected unless a separate sweep manifest is passed](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/engine_backtest_service.py#L622). In the reproduction, the real `execute_engine_backtest` pins fixture A; its existing pre-read progress callback performs a catalog-authorized production publication of valid bars B; the engine then returns `success=True`, chart closes **777 from B**, and a response `fixture_sha256` still identifying **A**. Auto-fetch and persistence/companion dispatch are explicitly disabled so no external state is involved. The callback makes the otherwise concurrent interleaving deterministic; it does not replace the engine or reader.
2. **Actual LEAN orchestration, simulated consumer:** [`post_launch` completes first](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L921), then [`_build_manifest` runs](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L953) and [hashes the current lake paths](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L1091), including [factor files](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L1100). Two cases drive the real `run_trusted_sample`: the fake launcher reads A and then publishes B through the real publication functions before returning. The persisted workspace manifest records B for both the minute-zip case and the factor-file case, although the simulated consumer read A. The fake launcher returns exit code 1, so this proves the orchestration's failure-manifest path without inventing LEAN output. Success uses the same subsequent manifest construction; that extension is static evidence, not an executed LEAN backtest.

Every replacement uses `atomic.publish_artifact` and `catalog_client.publish_under_lease`; only the connection/transaction boundary is fake. These are whole, valid artifact replacements, not partial ZIP reads or corruption injections. No corporate-action coverage assumption is needed.

**Why it matters:** Hashing before or after execution identifies bytes at the hash time. It does not establish that those were the bytes parsed. The Python response is already wrong about its own fixture identity; even if a later companion detects the changed file and refuses, it cannot repair that response. LEAN can record a later valid revision as if it had supplied the prior run. This undermines the specific existing claims while leaving the looser availability fingerprint entirely legitimate.

**Recommendation:** For paths that advertise exact input identity, bind the admitted digest to the actual read, as the existing manifest-bound sweep readers do. Give LEAN a stable declared-input generation/view for the whole run, or obtain consumption evidence that cannot be replaced by a later path hash. Use version retention, stable file handles/payloads where supported, or scoped immutable views; a periodic check around mutable paths is insufficient. If a path cannot establish this guarantee, label its hash as an observation of lake state rather than consumed input identity and refuse to use it as exact parity evidence.

**Confidence:** High for the successful Python result and manifest mismatch. LEAN runtime execution is simulated; only the real orchestrator, publication, and manifest functions execute. A demonstrated stable run mount or enforced read binding on these same active paths would change the conclusion; none is present in the examined path.

### R2 — Failed catalog completion does not quarantine promoted bytes from filesystem-only admission

**Claim:** If completion SQL or transaction commit fails after rename, the new canonical file remains while the row is non-complete with its previous receipt. Fresh LEAN filesystem preflight and fresh sweep snapshot capture accept that file without consulting publication status.

**Goal:** Correctness; architecture.

**Severity:** **High.** A failed publication can become an apparently valid new input without the committed receipt the authority model relies upon. A mixed set of prior derived artifacts and newly renamed source bytes can pass the same file-presence coverage checks.

**Evidence — reproduced under synthetic transaction failures:** [Production publication renames before completion SQL and commit](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/catalog_client.py#L877). The [filesystem wrapper](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/atomic.py#L254) does not restore the old canonical file after promotion. The two reproduced cases raise respectively during completion `execute` and transaction exit/commit. Both leave the fake row `fetching` with hash A, while the file holds B. The real [`resolve_lake_artifacts`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/lean_sidecar/lake_mount.py#L319) accepts the window, and the real minute reader returns B's close **777**. A newly captured [`DataSnapshot`](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/sweep/snapshot.py#L122) also hashes B and a bound reader then accepts it. Thus correct read-time hash binding does not itself prove the file was successfully published.

The fake transaction preserves pretransaction row state on the injected failure; it does not simulate PostgreSQL locking or run any SQL against a database. The filesystem rename is real. Rollback leaving a non-complete row is also the exact failure model documented by the production code and ADR. [Grid Search](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/grid_search/service.py#L324) and [Walk-Forward](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/walk_forward_study/service.py#L95) use that filesystem snapshot capture; LEAN's [active preflight calls the filesystem resolver](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L560).

**Why it matters — rationale examined:** [ADR 0049 A5](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md#L201) explicitly reasons: “A reader trusting the catalog never accepts that file, and the next writer to win the lease overwrites it.” That conditional is sound for readers that actually require a fresh complete catalog row. It does not hold for these filesystem-only readers. The same ADR's file-authority model relies on catalog verification, so file existence cannot simultaneously serve as publication-complete evidence. Writer fencing fixes competing-writer ownership; it does not make a filesystem rename roll back with SQL or make readers honor catalog state.

**Recommendation:** Make the publication commit boundary visible to every admission/read path. One design is immutable artifact generations plus a committed pointer/manifest that readers resolve and retain; another must offer equivalent complete-publication and lifetime guarantees. Keep the prior committed generation readable while replacement is in flight or failed. Do not expose a renamed but uncommitted generation merely because it occupies the canonical path. Preserve the existing writer-generation fencing; this is a reader/publication-visibility gap, not a reason to remove it.

**Confidence:** High for file visibility and admission behavior with both injected failure locations. Real PostgreSQL contention/crash recovery was not executed. A catalog/generation verification boundary before these actual reader paths, or a filesystem rollback/quarantine protocol, would change the conclusion.

## Positive controls and limits

| Examined path | Evidence and result |
| --- | --- |
| Sweep read binding | [Minute](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/sweep/snapshot.py#L202) and [daily](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/research/sweep/snapshot.py#L221) readers read once, hash that payload, and parse the same payload. The new test replaces the file after successful verification but before parsing: the reader still returns A, and the next read refuses B. Existing tests also cover changed files, unreceipted files, missing sessions, and minute/daily equality. This is positive evidence against a general claim that every snapshot reader races. |
| Atomic filesystem operation | Stage paths are scoped to request/worker/attempt; writes and directories are flushed; cross-filesystem promotion is refused; promotion uses `os.replace`. Readers receive whole artifacts. Existing local tests for staging, rename, path safety, refused publication and cleanup passed. No torn-file defect is alleged. |
| Writer fencing | [Publication validates status, owner, generation and expiry under a row lock](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/catalog_client.py#L877). [Restore explicitly applies only before new bytes are promoted](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/catalog_client.py#L933). This review found no replacement of the old zombie-writer race; database-backed lock tests were inspected but not executed. |
| Availability versus exact bytes | The [materialization contract](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/data_lake/run_materialization.py#L445) explicitly calls `availability_hash` the materialized lake state, a superset of consumed artifacts. Its existence/size and resolution-specific coverage checks are useful admission controls. R1 does not reinterpret this field as a byte-exact consumption guarantee. |
| LEAN compatibility staging | The [source receipt](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L743) and [copied-workspace receipt](https://github.com/tim1016/learn-ai/blob/10b5f31b529c8507bd19bb24015f3d85fa9aba43/PythonDataService/app/services/lean_sidecar_service.py#L603) both compare against the expected Python fixture. A persistent A→B change therefore gives a typed refusal on that companion path. R1 does not claim the companion silently accepts every replacement, or demonstrate a false successful parity verdict. |
| Sidecar orchestration | Existing tests passed for read-only lake mount selection, raw/adjusted root selection, refusal before workspace allocation, stale/unreachable launcher, isolated fixture replay, and factor/interest-rate contributions to snapshot hashes. Hash inclusion is present; R1 concerns when the included bytes are observed. |
| Snapshot capture/reuse | A previously captured bound snapshot rejects B; a newly captured one accepts whatever files it successfully hashes. R2 concerns publication provenance at capture, not the stronger per-cell read guarantee after capture. No claim is made that a snapshot must represent one simultaneous cross-symbol market revision. |

No broker/live state, existing lake, container, or real database was read or changed. No provider market-data endpoint was called. The review is limited to the pinned source plus temporary synthetic files, not observed production incidents. Exact effects on trades, portfolio P&L, and final LEAN normalization were not tested; the successful Python reproduction proves mismatched receipt versus consumed chart input.

## Reproductions and verification

Artifacts: [six synthetic interleavings](https://github.com/tim1016/learn-ai/blob/research/codex-2429-snapshot/docs/references/reproductions/codex_review_2429.py) and [execution guard](https://github.com/tim1016/learn-ai/blob/research/codex-2429-snapshot/docs/references/reproductions/codex_review_2429_guard.py). The branch is this ticket's report-only artifact location; production citations above are pinned to the reviewed commit. Passing characterizations establish the observed baseline behavior, not a fix.

From the isolated clone's `PythonDataService` directory:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HOME=/Users/inkant/codex-review-20260924/stocks \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  POLYGON_API_KEY=synthetic-review-key DATA_PLANE_CONTROL_SECRET='' \
  TMPDIR=/Users/inkant/codex-review-20260924/stocks/.review-tmp \
  /Users/inkant/learn-ai/PythonDataService/.venv/bin/python -B \
  ../docs/references/reproductions/codex_review_2429_guard.py \
  -m pytest --noconftest -p pytest_asyncio.plugin -o addopts='' \
  --basetemp=/Users/inkant/codex-review-20260924/stocks/.review-tmp/2429 \
  ../docs/references/reproductions/codex_review_2429.py \
  tests/unit/data_lake/test_atomic.py tests/research/sweep/test_snapshot.py \
  tests/lean_sidecar/test_lake_mount_service.py
```

Final result: **40 passed** (6 review cases; 15 atomic, 9 snapshot, and 10 sidecar controls). Ruff over the reproduction files and all `PythonDataService/app/` and `PythonDataService/tests/` passed. An initial review-harness attempt incorrectly treated a dataclass as a Pydantic model; that test-only construction was corrected. A focused rerun emitted the installed Starlette `python_multipart` pending-deprecation warning; no product finding is inferred.

Imports, fixtures and initialization paths were inspected. The audit guard installs before app imports, rejects network connects/DNS/binds and subprocess launch, rejects `.env`, `live_runs` and audit-archive reads, permits only read-only venv access under the main checkout, and confines test writes to the isolated review tree. Plugin autoload/root conftest are bypassed. The launcher, catalog connection and persistence boundaries are fake; the production engine, writers, parsers, admission, orchestration and manifest functions under discussion are real. No fixes or PRs were created.

## Bounded followups

1. If addressed, verify a single declared input generation through admission, publication failure, full run and replay, with a process-independent consumer stand-in. Extend the existing same-buffer sweep control to minute, daily, factor and metadata inputs without touching live data.
2. Check any future run-level guarantee separately from the accepted A3 scope withdrawal. No owner answer is needed to correct the accuracy of receipts already advertised as exact. Requiring universal durable snapshots or retaining every historical generation indefinitely would be a new owner decision, not an implied recommendation of this report.

This ticket is ready for parent publication; further failure-mode enumeration is not necessary to establish these two findings.
