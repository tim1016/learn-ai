# Research artifact seam

**Status:** Design ratified — implementation pending (no PRs yet)
**Last reviewed:** 2026-05-26
**Pairs with:** `docs/architecture/engine-authority-map.md` (the rows for `runs`, `walk_forward`, `monte_carlo`, `baselines`), `docs/references/run-ledger.md` (the canonical-JSON hash that PR 4 must preserve byte-for-byte)

This document answers exactly one question: **what is the shared seam under the four research-run phases, and how do we cut over to it without breaking on-disk artifacts, replay hashes, or routers?**

## The problem in one paragraph

Today the four research phases under `PythonDataService/app/research/` (`runs/`, `walk_forward/`, `monte_carlo/`, `baselines/`) each ship a sibling `storage.py` — same regex on the id, same atomic tmp-then-rename, same path-traversal guard, same Pydantic load-and-validate, same `list_*(parent_run_id=...)` scan. ~720 LOC of near-identical code with three things that actually vary across phases: subdirectory, filenames (`ledger.json` vs `config.json`), and the parent-run-id field name. The interface each phase's storage module exposes is nearly as complex as its implementation — the module is **shallow** in the sense of [Language § Depth](../../.claude/skills/improve-codebase-architecture/LANGUAGE.md). The same shallowness is paid again in `app/routers/{research_runs,walk_forward,monte_carlo,baselines}.py`, which re-implement the same three HTTP endpoints over the same storage trio.

A fifth phase (`robustness`) is already announced in `engine-authority-map.md` row 31. Adding it under the current shape pays the same ~180 LOC of storage boilerplate again.

## The seam — one ArtifactStore, one descriptor per phase

Deepen by extracting one `app/research/artifact/` module that owns the persistence mechanics. Each phase keeps its Pydantic types, its runner, its validation, and its public exception classes — but no longer carries its own copy of save/load/list.

```python
# app/research/artifact/descriptor.py
@dataclass(frozen=True)
class ArtifactDescriptor:
    subdir: str                                  # e.g. "runs", "walk-forward", "monte-carlo", "baselines"
    id_pattern: re.Pattern[str]                  # validates and constrains the id
    config_filename: str                         # "ledger.json" for runs, "config.json" elsewhere
    result_filename: str                         # "result.json" everywhere today
    parent_run_id_extractor: Callable[[BaseModel], str | None]
    # Optional: phase opts in to canonical-JSON hashing
    hash_payload: Callable[[BaseModel], str] | None = None
    # Phase-named exception classes (see "Errors" below)
    not_found_error: type[ArtifactNotFoundError] = ArtifactNotFoundError
    already_exists_error: type[ArtifactAlreadyExistsError] = ArtifactAlreadyExistsError
    corrupt_error: type[ArtifactCorruptError] = ArtifactCorruptError

# app/research/artifact/store.py
class ArtifactStore:
    def __init__(self, descriptor: ArtifactDescriptor, *, root: Path | None = None): ...
    def save(self, config: BaseModel, result: BaseModel, *, replace: bool = False) -> Path: ...
    def load(self, artifact_id: str, *, config_type: type[C], result_type: type[R]) -> tuple[C, R]: ...
    def list_ids(self, *, parent_run_id: str | None = None, since_ms: int | None = None, limit: int | None = None) -> list[str]: ...
```

Each phase declares its descriptor at module level:

```python
# app/research/monte_carlo/__init__.py
MONTE_CARLO_ARTIFACT = ArtifactDescriptor(
    subdir="monte-carlo",
    id_pattern=re.compile(r"^[0-9a-f]{32}$"),
    config_filename="config.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda cfg: cfg.parent_run_id,
    not_found_error=MonteCarloNotFoundError,
    already_exists_error=MonteCarloAlreadyExistsError,
    corrupt_error=MonteCarloCorruptError,
)
```

The phase's `storage.py` shrinks to a thin construction site:

```python
# app/research/monte_carlo/storage.py (after the refactor)
def _store(root: Path | None = None) -> ArtifactStore:
    return ArtifactStore(MONTE_CARLO_ARTIFACT, root=root)

def save_monte_carlo(config, result, *, root=None, replace=False):
    return _store(root).save(config, result, replace=replace)

# load_monte_carlo, list_monte_carlos analogous
```

Runners call `save_X` exactly as they do today. Routers stay transport-only. **No router changes unless mechanically required by an exception rename.**

## The seven decisions

| # | Decision | What it locks in |
|---|---|---|
| 1 | **Heterogeneous on-disk layout retained.** The descriptor parametrises `(subdir, id_pattern, config_filename, result_filename, parent_run_id_extractor)`. `runs/` keeps `ledger.json`; the others keep `config.json`. The names are not arbitrary — `engine/live/run_ledger.py:55-56` calls the ledger out as an *immutable identity object*; the others are mutable inputs. Collapsing would erase a semantic distinction the codebase already encodes. | Zero migration cost. No bytes change on disk. The first PR is behavior-preserving consolidation, not a schema migration. |
| 2 | **Hashing is opt-in per phase via a callback hook.** `runs/hashing.py` remains the canonical implementation of `RunLedger` canonical-JSON SHA-256. The artifact module exposes `hash_payload: Callable | None` on the descriptor; if absent, no hashing happens. | Existing replay addresses stay stable byte-for-byte. Other phases can opt in later (when, e.g., walk-forward folds become replay-addressable) without breaking the runs/ contract. |
| 3 | **Parent-run-id listing is scan-based and deduplicated in the artifact module.** No physical index. A real index buys nothing today — the duplicated *code* is the pain, not duplicated *work*. | No invalidation, repair, migration, or corruption semantics to design. If profiling later shows the scan is a problem, an index slots in behind the same `list_ids` interface. |
| 4 | **Phase owns Pydantic config/result types, runner logic, validation semantics, and any phase-specific math.** The artifact module owns path construction, id validation, atomic write, load-and-validate-into-provided-Pydantic-types, list/filter, the optional hash hook, and the parent-run-id extractor. | Phase semantics stay where they belong. The seam is purely *persistence mechanics*. Runners can call the store directly; routers stay transport-only. |
| 5 | **Phase-named exception classes stay first-class**, supplied through the descriptor, but inherit from shared bases. | Existing tests (`pytest.raises(WalkForwardNotFoundError)`) and routers (which map specific exceptions to specific HTTP codes) continue to work unchanged. New common code can `except ArtifactError`. |
| 6 | **Descriptor-backed `ArtifactStore` bound at construction.** Module-level `XX_ARTIFACT = ArtifactDescriptor(...)`, constructed as `ArtifactStore(XX_ARTIFACT, root=...)` at the call site. No kwargs-at-each-call — that repeats the very policy we're consolidating. | The phase's on-disk identity is grep-able from one place. The store is dependency-injectable for tests. |
| 7 | **Strangler migration, one phase per PR.** `monte_carlo` first; `baselines` and `walk_forward` in either order; `runs/` last. Each PR preserves on-disk layout, public exceptions, runner/router behaviour, and proves behaviour-equivalence for save/load/list/parent-id filtering. | Each PR has a small review surface and minimal test churn. The shared bases and descriptor-backed store move the architecture forward without paying migration cost, route churn, or hash-risk up front. |

## Shared base errors

```python
# app/research/artifact/errors.py
class ArtifactError(Exception): ...
class ArtifactNotFoundError(ArtifactError, LookupError): ...
class ArtifactAlreadyExistsError(ArtifactError, FileExistsError): ...
class ArtifactCorruptError(ArtifactError, RuntimeError): ...
```

Each phase re-bases its existing exceptions onto these:

```python
# app/research/monte_carlo/result.py (or wherever the exceptions live today)
class MonteCarloNotFoundError(ArtifactNotFoundError): ...
class MonteCarloAlreadyExistsError(ArtifactAlreadyExistsError): ...
class MonteCarloCorruptError(ArtifactCorruptError): ...
```

The artifact module raises whichever class the descriptor specifies. Catch-by-name in routers stays valid; catch-by-base becomes available to new code.

## PR plan

| PR | Scope | Why this order |
|---|---|---|
| **1** | Introduce `app/research/artifact/` (descriptor, store, shared base errors) with full unit tests. Migrate **`monte_carlo`** as the first consumer. | `monte_carlo` is the only phase that doesn't depend on `runs/storage` even transitively — it loads a parent `RunLedger` via the existing `runs/` reader but persists *its own* artifact independently. Smallest blast radius for the first proof. |
| **2** | Migrate **`baselines`** (or `walk_forward` — either order). | Both phases persist child specs/folds as `RunLedger` records via `runs/storage.save_run` *today*. Migrating these in PR 2 still leaves the `runs/` write path untouched — the phase's own top-level artifact moves to the new store while child runs continue through the old runs/ storage code. |
| **3** | Migrate the other of **`baselines`** / **`walk_forward`**. | Same shape as PR 2. |
| **4** | Migrate **`runs/`** last. | This is the highest-risk migration: `ledger.json` is the canonical-JSON hash payload, and any byte change to the on-disk record invalidates replay addresses for every existing run. The descriptor's `hash_payload` hook is exercised for the first time here; the test bar is *byte-identical persisted records against pre-migration goldens*. |

### Per-PR acceptance bar

Each PR must demonstrate, before merge:

- On-disk layout for the migrated phase is byte-identical to pre-PR for the same input. Persist a golden artifact pre-PR, replay save through the new store, diff bytes.
- All existing public exception classes for the phase still exist and are still raised in the same situations.
- Runner code path is unchanged (or changed only mechanically — e.g. the import line).
- Router code path is unchanged.
- `save / load / list_ids` behaviour matches pre-PR for the parent-run-id filter, the since-ms filter, and the limit.

## Explicitly out of scope

- **Normalising the on-disk subdirectory naming.** `runs/`, `walk-forward/`, `monte-carlo/`, `baselines/` stay heterogeneous. A future migration could normalise to `<root>/<phase>/<id>/` if the asymmetry becomes load-bearing somewhere it isn't today.
- **Physical parent-run-id index.** Re-evaluate only if `list_ids(parent_run_id=...)` becomes a measured hotspot.
- **GraphQL passthrough.** None of the research phases ship a GraphQL surface yet. The artifact seam doesn't change that — when GraphQL passthrough lands (per `engine-authority-map.md` row 29), it composes against the existing router layer.
- **Unifying the FastAPI routers** (`research_runs.py`, `walk_forward.py`, `monte_carlo.py`, `baselines.py`). That was candidate #3 in the architecture review and is queued behind this work. The router-factory shape is much smaller if the storage/artifact seam below it is already unified, which is why this PR sequence ships first.
- **Backend HTTP-passthrough consolidation (candidate #1 in the architecture review).** Deferred. Concerns a different layer (.NET adapters) and a different policy surface (boundary semantics: 422→typed validation error, correlation IDs, operation-class timeouts). To be drafted as a separate ADR when prioritised.

## Discovered during PR 1 implementation (2026-05-26)

Findings from the three parallel dispatches (implementation + test-surface survey + comparative anatomy of the four `storage.py` files). These do **not** reopen the seven decisions; they refine the descriptor surface and flag two open questions for PR 4.

### Refinements baked into PR 1 — future PRs should follow

1. **Exception classes live in a dedicated `<phase>/errors.py`**, not in `result.py` or `storage.py`. The artifact store's `store.py` needs to import the descriptor's `*_error` types without creating a circular import; pulling the exceptions into a new `errors.py` per phase is the cleanest cut. `storage.py` re-exports the names so existing callers don't move.
2. **`list_ids` does not pre-filter directory names by `id_pattern`.** The existing `test_list_skips_corrupt_config` semantics (warn-and-skip on a debris dir) are preserved. The `id_pattern` regex gates `save` and `load` only (user-controlled ids); listing is best-effort and logs at WARN when a payload won't parse.
3. **The descriptor has an explicit `id_field: str` and the store uses `getattr(config, descriptor.id_field)`.** An earlier auto-scan-by-regex implementation crashed in normal use (`MonteCarloConfig` has both `monte_carlo_id` and `parent_run_id`, both 32-hex; auto-scan couldn't disambiguate). Fixed in commit `1146a95` on the PR 1 branch — `_extract_id` was renamed `_get_id` and now does explicit lookup with `id_pattern` only validating the resulting value. The byte-equivalence test's `parent_run_id` fixture was hex-tightened from `"p"*32` (outside `[0-9a-f]`) to `"b"*32` so the test now actually exercises the two-hex-id path that would have caught the bug.
4. **`parent_run_id_extractor` is invoked over a `_PayloadView`** (a lightweight attr-access wrapper over the raw JSON dict) during `list_ids`, not over a fully-hydrated `BaseModel`. This avoids parsing + validating every artifact's config just to read one field — material when listing thousands. The descriptor's type hint relaxes from `Callable[[BaseModel], str | None]` to `Callable[[Any], str | None]`; typical extractors (`lambda cfg: cfg.parent_run_id`) work unchanged under duck typing.
5. **Phase-specific list filters stay in the phase's thin delegator.** `list_monte_carlos` accepts a `method` filter not present in the descriptor; the delegator calls `store.list_ids(...)` for the generic filters, then re-parses surviving configs to apply phase-specific filters before `limit` truncation. Pattern: generic filter → phase filter → limit.
6. **Public list functions return `list[Config]`, not `list[str]`.** The store's `list_ids` returns ids; the phase delegator hydrates each surviving artifact into its Pydantic config (or its config + result tuple, depending on the existing signature) and returns the public-API shape. Cost: one extra JSON parse per matched id. Acceptable.

### Resolved during PR 1 follow-up (commits `1146a95` + `ed42ed6` on the branch)

The three questions that surfaced during PR 1 are now decided. Future PRs implement against these:

1. **`default_artifacts_root` home — moved now.** The function lives at `app/research/artifact/root.py` (re-exported from `app/research/artifact/__init__.py`). `runs/storage.py` re-exports the symbol so the still-unmigrated `walk_forward/storage.py` and `baselines/storage.py` imports keep working until their own PRs. The post-migration `monte_carlo/storage.py` imports directly from `app.research.artifact.root` — it does not lean on the compat shim. PR 2 and PR 3 migrate their phase's import as part of the same diff; PR 4 then deletes the compat re-export when it rewrites `runs/storage.py`. `ARTIFACTS_ROOT_ENV` is re-exported alongside `default_artifacts_root` (the runs router and one test still reference it).
2. **`log_tag` is a required `str` field on `ArtifactDescriptor`.** Every `logger.warning` the store emits during `list_ids` formats as `[<log_tag>] ...` to preserve operator grep patterns. Production tags: `MC`, `WF`, `BASELINES`, `RUNS`. Test descriptors should use a visibly-synthetic tag like `PHX` so test logs don't fingerprint as production.
3. **`subdir=""` is the runs-phase shape, kept.** `ArtifactDescriptor.subdir: str` accepts empty string. Test coverage explicitly exercises the flat layout (`tests/research/artifact/test_store.py::test_store_with_empty_subdir_writes_artifact_at_root_directly`) so reviewers see it's intentional, not a missing parameter.

### Confirmed by the comparative anatomy (no action needed)

- All four phases use the same `id_pattern` (`r"^[0-9a-f]{32}$"`), the same atomic-write helper, the same path-traversal guard, the same `created_at_ms` field for `since_ms` filtering, the same `parent_run_id` field name, and the same save-order (result first, config second). The descriptor's six axes (`subdir, id_pattern, config_filename, result_filename, parent_run_id_extractor, hash_payload`) cover every axis of variation in the four existing implementations. No seventh axis surfaced.
- No test in the repo imports a private helper from any phase's `storage.py`. No test mocks a storage symbol. The blast radius of the rewrite is just the public `save_X` / `load_X` / `list_X` signatures plus the three exception classes per phase — all of which PR 1 preserves.

## Cross-references

- `docs/architecture/engine-authority-map.md` — rows 29-32 enumerate the four phases this seam unifies.
- `docs/references/run-ledger.md` — canonical-JSON hashing scheme. PR 4 must preserve every byte.
- `docs/references/walk-forward.md`, `docs/references/monte-carlo.md`, `docs/references/baselines.md` — per-phase semantics. Untouched by this work.
- `.claude/skills/improve-codebase-architecture/LANGUAGE.md` — vocabulary for **module / interface / implementation / depth / seam / adapter / leverage / locality** used throughout this doc.
