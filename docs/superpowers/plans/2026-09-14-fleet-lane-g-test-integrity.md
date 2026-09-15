# Lane G: fleet test-suite integrity (#2074) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three tests-only PRs: make the two conformance fakes canonicalize differently and fix everything the collapse hid; fix three latent flakes; make the B-scoped fixtures falsifiable and rename 33 tests whose names outran their assertions.

**Architecture:** See the lane report below; every task carries its own files, failing test, implementation and commit.

**Tech Stack:** Python 3.12 / FastAPI / pytest (PythonDataService), Angular 22 / Vitest (Frontend), Podman Compose, GitHub Actions.

**Spec:** the GitHub issues named in the title, plus `docs/broker-clerk-fleet-authority.md` §6 (trust register). Master sequencing, owner decisions, deploy windows and the delivery protocol live in `2026-09-14-fleet-trust-register-fixes.md` — read it first; its Global Constraints apply to every task here.

## Global Constraints

Inherited verbatim from `2026-09-14-fleet-trust-register-fixes.md` § Global Constraints. In particular: never switch branches in `/Users/inkant/learn-ai` (the clerks bind-mount it); every `./restart.sh` drops both clerks; import-time changes need a no-bot window; run the thermo review once before the first push; fleet tests run unsandboxed.

## Corrections applied by the integrating session

- Item 19 defers to Lane D Task 5(c): production_adapter() is deleted there, so the strengthened test asserts absence of the function, not BrokerNotSupported.
- PR 3 and Lane D PR C both edit test_b_scoped_contracts.py; land one, rebase the other.
- Task 1's `canonical_rule` default is corrected below to match shipped code: review of PR #2085 removed the alpha-shaped class default entirely (a bare `FakeProviderAdapter("fake_beta")` was silently inheriting alpha's rule), making `canonical_rule` a required field and `fake_alpha()`/`fake_beta()` the only constructors. Carried from #2113 (Codex P2, originally #2085).

---

*The report below was produced by an Opus planning agent on 2026-09-14 against origin/master `e2fdb23f` plus the branches of PR #2081 and PR #2078. Every file:line claim was verified by that agent at that time; re-verify after those PRs merge.*

---

I have everything I need. Here is the plan.

---

# Fleet test-suite integrity — verification and tests-only remediation plan (ISSUE #2074)

## 0. Baseline

| Fact | Result |
|---|---|
| Command | `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/fleet tests/contracts -q` (unsandboxed) |
| Result | **315 passed, 0 failed, 72.32s** on `origin/master` working tree |
| Collected | `tests/broker/fleet` = **222 tests**; `tests/contracts` = 93 |
| With PR #2081 | adds 2 fleet tests + 2 fleet tests… precisely: `test_fleet_boot_lost_configuration.py` (2), `test_identity_middleware_role_scope.py` (2), `tests/contracts/test_restart_script_compose_ownership.py` (3), `test_restart_script_reap_behaviour.py` (3) → **226 fleet / 99 contracts / 325 total** |
| Issue's "227 tests" | **DRIFTED** — 222 today, 226 with #2081. Close; the drift is ±1–5, not material. |
| Brief's "expected 357 passed" | **WRONG** — the real number is 315 (master) / 325 (with #2081). Nothing is failing; the count in the brief is stale. |

`PythonDataService/tests/broker/fleet` also lints clean today (`ruff check tests/broker/fleet/` → *All checks passed*), so every proposal below must keep it that way.

---

## 1. Verified facts

| # | Claim from #2074 | Verdict | Evidence |
|---|---|---|---|
| 1 | Both fakes canonicalize identically at `conftest.py:127`, never overridden | **CONFIRMED** | `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/conftest.py:127` `canonical_rule: Callable[[str], str] = lambda raw: raw.strip().upper()`; `fake_alpha()` (`:158-164`) and `fake_beta()` (`:167-173`) pass only `provider_id`/`capabilities`/`declared_operations` |
| 2 | `test_identical_raw_account_ids_coexist_across_providers` asserts the two forms are EQUAL | **CONFIRMED** | `test_assignments.py:55` `assert first.canonical_external_account_id == second.canonical_external_account_id` |
| 3 | `test_compatibility_evidence_separates_unauthorized_and_not_found_responses` proves they COLLAPSE | **CONFIRMED** | `test_lane_runtime.py:315`; the 403 (`/assets`) and the 404 (`/activities/missing`) land in one bucket — `test_lane_runtime.py:347-353` asserts `{"route_family": "brokers_lane_extras", "response_class": "4xx", "count": 2}` |
| 4 | B-scoped served identity is copied from the registry | **CONFIRMED** | `test_b_scoped_contracts.py:271` `self.identity["routing_epoch"] = session.routing_epoch`; `clerk_id` copied at `:258`, `binding_generation: 3` hard-matched at `:260` vs `:209` |
| 5 | The "provider receipt" is derived from the coordinator's own idempotency key | **CONFIRMED** | agent handler `test_b_scoped_contracts.py:140` `"receipt_id": f"provider/command-{body.get('idempotency_key')}"`; asserted at `:472`, `:482`, and again at `:924` |
| 6 | `verify_identity_echo` can never fail for a coordinator-side bug | **CONFIRMED at integration level, PARTIALLY WRONG at unit level** | No integration path can diverge (see #4). But `test_a2_alpaca_lane.py:514-552` unit-tests `verify_identity_echo` with synthesized mismatched headers and it *does* raise. So the function is falsifiable; the *B-scoped fixture* is not. |
| 7 | `delivery_for` ignores both args; the 503 is a TCP failure to port 9 | **CONFIRMED** | `test_b_scoped_contracts.py:232-235` returns a fixed `HttpLaneDelivery(base_url=agent_base_url, …)`; `:572` `_RealServer(_coordinator_app(lane, "http://127.0.0.1:9"))`. The production check that *would* catch it is `app/broker/fleet/routing.py:643-650` (`coordinator_delivery_for`), which this fixture never calls. Both paths emit `reason == "clerk_unreachable"` / 503 (`routing.py:212` vs `routing.py:646`), so `:582` cannot tell them apart. |
| 8 | No fleet module reads `os.environ` | **CONFIRMED** | `grep -rn "os.environ\|getenv" app/broker/fleet/` → zero hits. The `FAKE_ALPHA_API_KEY_ID` / `FAKE_ALPHA_API_SECRET_KEY` canaries (`test_secret_absence.py:22-24`, `:31-32`) are unfalsifiable by construction. |
| 9 | Only 3 of 8 sinks are swept | **CONFIRMED** (see §5 for the enumeration) |
| 10 | Wall-clock race at `test_fleet_cli.py:250` | **CONFIRMED, and I can name it** | The operator receipt is stamped `now_ms_utc()` at `:338`; `window_end_ms` is forced to `start_snapshot.captured_at_ms + 1` at `:297`; `compatibility_retirement.py:148` refuses when `receipt_issued_at_ms < window_end_ms`. If the stretch from `:265` (first `compatibility-snapshot`) to `:338` completes inside one millisecond, the test fails. Nothing in the test pins a clock. |
| 11 | `< 0.1s` timing assert at `test_lane_runtime.py:387` | **CONFIRMED** | test begins `:387`; the assert is `test_lane_runtime.py:406` `assert time.monotonic() - started_at < 0.1`, racing a `time.sleep(0.2)` injected at `:395` |
| 12 | Global `os.replace` / `os.fsync` monkeypatch at `:368` / `:562` | **CONFIRMED** | `lane_runtime.py:16` is a plain `import os`, so `app.broker.fleet.lane_runtime.os` **is** the process-global `os` module; `monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", …)` mutates `os.replace` process-wide for the duration of the test |
| 13 | 6 of 227 tests create real contention; 2 across separate store connections | **CONFIRMED (I count 5 / 1)** | Real threads: `test_assignments.py:276` (separate `FleetRegistryStore.open` per thread — the one true cross-connection race), `test_assignments.py:310`, `test_reviewer_fences.py:71`, `test_routing_attempts.py:126`, `test_admission_probes_2026_09_13.py:407` — the last four all share one `fleet_service`. `test_reviewer_fences.py:245` opens a second store but uses a deterministic monkeypatched interleave, not threads. |

**Extra facts the issue did not name, all verified:**

- `app/broker/fleet/provider.py:297` `PRODUCTION_PROVIDER_ADAPTERS = {}` is vestigial: `service.py:160` falls back to it only when no adapters are injected, and `production_adapter()` (`provider.py:348`) has **zero callers outside its own module and two tests**. Two tests (`test_import_isolation.py:95-105`, `test_a2_alpaca_lane.py:97-99`) pin it as a *Phase-2 gate* that closed when Alpaca landed in `app/broker/fleet_composition.py`.
- `app/broker/fleet/provider.py:283` `validate_served_context` has **no production caller** (only `fleet_adapter.py:759` defines it and `test_a2_alpaca_lane.py:71` calls it directly). The fake's `served_context_refusals` knob (`conftest.py:129`, `:151-155`) is set by **no test at all** — dead fixture surface. The docs PR #2078 independently ranks this #4 on its trust-raising list.
- `test_registry_contains_no_custody.py`: `_identifier_tokens` (`:48-50`) splits on `[a-z0-9]+`, so the two underscore-bearing entries in `_FORBIDDEN_NAME_FRAGMENTS` — `"api_key"` (`:41`) and `"endpoint_url"` (`:44`) — **can never match a token**. 2 of 15 canaries in that sweep are structurally unfirable.
- `docs/broker-clerk-fleet-authority.md` on `origin/docs/broker-clerk-fleet-authority` already cites `tests/broker/fleet/conftest.py:127` by line and says "provider-qualified assignment is effectively untested". This lane closes a gap that doc names.

---

## 2. (A) The divergence list

Format: `file::test — name claims X, body asserts Y — fix`.

### A1. Rename-only (12)

1. `test_provider_conformance.py::test_n_clerks_across_two_providers_run_concurrently` — name claims concurrency; body (`:35-72`) provisions and binds six lanes in a **sequential `for` loop**, no threads — *rename to* `test_six_clerks_across_two_providers_hold_distinct_volumes_and_one_registry`.
2. `test_reviewer_fences.py::test_retirement_races_a_reservation_without_leaving_an_orphan` — "races"; body (`:259-283`) monkeypatches `store.transaction` for a deterministic interleave — *rename to* `test_a_retirement_landing_inside_a_reservation_refuses_without_leaving_an_orphan`.
3. `test_routing_attempts.py::test_a_racing_downgrade_after_delivered_refuses_typed` — "racing"; body (`:185-189`) monkeypatches `read_routing_receipt` to serve a stale read — *rename to* `test_a_stale_pre_delivery_read_cannot_downgrade_a_delivered_outcome`.
4. `test_routing_attempts.py::test_dispatch_is_one_way_and_recorded_before_settlement` — "recorded before settlement"; the body's own comment (`:121-123`) says settling an unmarked attempt is legal and never asserts ordering — *rename to* `test_a_dispatched_attempt_can_never_present_as_un_sent`.
5. `test_lane_runtime.py::test_compatibility_evidence_separates_unauthorized_and_not_found_responses` — name claims 403 ≠ 404; body proves 403 + 404 = one `4xx` bucket of count 2. What it *does* prove is the docstring's claim, failures ≠ successes — *rename to* `test_compatibility_evidence_separates_failed_probes_from_successful_reads`. (Splitting 401/403 from 404 would need a `response_class` change in `lane_runtime.py`; out of scope for a tests-only lane — see §4.)
6. `test_compatibility_retirement.py::test_evaluate_retirement_requires_all_nontraffic_evidence` — "requires all"; body (`:124-133`) is the happy path only: complete evidence ⇒ `decision == "eligible"`. The three "requires" are proved by `:137`, `:150`, `:163` — *rename to* `test_complete_evidence_evaluates_eligible_with_twenty_zero_route_deltas`.
7. `test_schema_migration.py::test_an_incomplete_v1_schema_without_a_registered_path_refuses` — "without a registered path"; body (`:157-183`) amputates a table from a v1 registry that *does* have a registered v1→v2 path — *rename to* `test_an_amputated_v1_registry_refuses_rather_than_producing_a_broken_v2`.
8. `test_operation_catalog.py::test_alpaca_catalog_covers_every_canonical_desk_read` — "every"; body (`:129-147`) pins an inline 7-entry dict and its `==` is keyed by `expected`, so extra operations are invisible — *rename to* `test_alpaca_declares_the_seven_desk_reads_at_their_pinned_routes`.
9. `test_directory_and_aggregation.py::test_every_entry_carries_broker_clerk_identity_and_no_internal_secrets` — "no internal secrets" (plural); body (`:65-70`) checks the field allowlist and exactly one value, the worker key — *rename to* `test_every_entry_carries_only_the_allowed_fields_and_never_the_worker_key`.
10. `test_operation_catalog.py::test_catalog_validation_refuses_ambiguous_and_incoherent_declarations` — a near-synonym of `test_ambiguous_or_malformed_catalogs_refuse` two functions above it; the two split by *shape rules* vs *cross-field coherence* but neither name says so — *rename to* `test_catalog_validation_refuses_cross_field_incoherence` and rename `:74` to `test_catalog_validation_refuses_malformed_operation_shapes`.
11. `test_provisioning_and_volume.py::test_provisioning_an_unknown_production_provider_fails_closed` — the body passes `provider_adapters={}` with the stale comment "`# the production set is empty in this slice`" (`:47`); the production set has held Alpaca since A2 — *rename to* `test_provisioning_against_an_empty_adapter_set_fails_closed` and delete the stale comment.
12. `test_a2_alpaca_lane.py::test_a_wrong_identity_echo_is_an_uncertain_outcome` — "uncertain outcome" (`ClerkRoutingOutcomeUnknown`); body (`:516-552`) asserts `DeliveryIdentityMismatch` — and requests the `agent_server` fixture (`:514`) which starts a real uvicorn server the body never touches. *Rename to* `test_verify_identity_echo_refuses_a_wrong_or_missing_echo` **and drop the unused fixture parameter.**

### A2. Strengthen-assertion (14)

13. `test_assignments.py::test_identical_raw_account_ids_coexist_across_providers` — name says provider-qualified identity; `:55` asserts the canonical forms are **equal**. *Fix:* assert inequality **and** coexistence (task T2).
14. `test_b_scoped_contracts.py::test_commands_without_an_approved_endpoint_refuse` — name claims the approval check; the 503 is a TCP refusal. *Fix:* task T5.
15. `test_secret_absence.py::test_no_secret_reaches_a_row_a_payload_or_a_log` — name claims three sinks and a whole ceremony; the canaries can never enter any sink. *Fix:* task T6.
16. `test_registry_store.py::test_foreign_key_pins_assignments_and_sessions_to_real_clerks` — "assignments **and** sessions"; body (`:110-120`) inserts one orphan **assignment** only. *Fix:* add an orphan `clerk_sessions` insert asserting `sqlite3.IntegrityError`.
17. `test_registry_contains_no_custody.py::test_no_timestamp_column_is_textual` — docstring claims "INTEGER ms **within the canonical bound**"; body (`:96-110`) checks the declared type only, and only for columns ending `_ms`. *Fix:* assert the sweep covered ≥1 column per table that carries time, and assert no column whose name contains `at`/`time` escapes the `_ms` suffix convention.
18. `test_registry_contains_no_custody.py::test_no_column_name_carries_a_custody_or_secret_fragment` — 2 of 15 fragments (`api_key`, `endpoint_url`) can never fire. *Fix:* match multi-token fragments against the column's normalized token *sequence*, not the token set, and add a self-test that a synthetic `api_key` column is caught.
19. `test_import_isolation.py::test_the_production_adapter_registry_stays_empty_until_phase_2` — the phase gate it names is closed; it now pins a vestigial constant. *Fix:* rename to `test_the_fleet_packages_own_registry_is_not_the_production_composition` and add `assert set(production_provider_adapters()) == {"alpaca"}` so the test states the *real* invariant (composition, not the package, owns providers). Keep the `production_adapter("alpaca") → BrokerNotSupported` assertion; delete the duplicate at `test_a2_alpaca_lane.py:97-99`.
20. `test_directory_and_aggregation.py::test_partial_aggregation_reports_each_lane_without_omission_or_substitution` — "without substitution" is never asserted. *Fix:* `assert "value" not in lanes[1]` — a failed lane must carry no value at all.
21. `test_directory_and_aggregation.py::test_capability_evidence_differs_by_provider_and_undeclared_actions_refuse` — the two `_live(...)` lanes at `:90-91` are never read (`require_capability` is broker-scoped, `service.py`), so half the setup is dead and "evidence differs" is never asserted. *Fix:* delete the two `_live` calls and assert the two adapters' declared capability sets differ (`FAKE_ALPHA_CAPABILITIES != FAKE_BETA_CAPABILITIES`) before the refusals.
22. `test_internal_http.py::test_multi_data_lines_join_and_comments_are_ignored` — the loopback server (`:61-99`) never writes a comment line. *Fix:* add `: keepalive\n` inside the `/events` frame and assert the parsed event is unchanged.
23. `test_internal_http.py::test_cancellation_propagates_to_the_open_stream` — "disconnects the server" never asserted; `_InternalServer.disconnect_seen` (`:45`, set at `:96`) is asserted by **no test in the file**. *Fix:* `await asyncio.wait_for(internal_server.disconnect_seen.wait(), timeout=5)`.
24. `test_internal_http.py::test_a_leading_bom_is_stripped_and_truncated_utf8_refuses` — `:286` `assert events == [events[0]]` is self-referential (and IndexErrors on empty). *Fix:* `assert len(events) == 1`.
25. `test_internal_http.py::test_sse_events_arrive_incrementally_over_a_real_socket` — `:125` asserts `first_event_seen.is_set()`, which the consumer's own arrival guarantees. The server at `:71-74` sets the event then `await`s the event it just set — a no-op. *Fix:* give the server a separate `hold_open` event the test controls, and assert it is *not* set at the moment the first event arrives.
26. `test_schema_migration.py::test_the_migration_is_idempotent_and_reopening_changes_nothing` — "changes nothing" is asserted for `schema_version` and `registry_id` only. *Fix:* snapshot every row of every allowed table before/after the reopen and assert equality.
27. `test_sessions_and_routing.py::test_routing_attempts_correlate_and_never_replace_upstream_evidence` — "never replace upstream evidence" is never exercised here (it lives at `test_routing_attempts.py:216`). *Fix:* rename to `test_routing_attempt_identity_is_per_lane_and_idempotent` (rename-only in effect; listed here because the second half of the claim is genuinely untested in this file).
28. `test_b_scoped_contracts.py::test_stream_events_carry_and_verify_provenance` — "verify"; the body (`:511-518`) substring-scans the raw SSE text. The coordinator's `validate_event_identity` is what verifies, and it is never asked to fail here. *Fix:* covered by task T4 (the flipped-identity case already exists at `:521`; add the positive assertion that the epoch in the frames equals the **agent-derived** epoch, not the registry's).
29. `test_reviewer_fences.py::test_an_adapter_registered_under_another_provider_id_refuses` — `:166-170` is `provision_clerk(...) or misregistered._adapter("fake_beta")`; `provision_clerk` always raises, so the right operand is unreachable dead code, and the `volume_root` it names is never created. *Fix:* split into two explicit `pytest.raises` blocks — one for `provision_clerk`, one for `_adapter`.

### A3. Delete-as-duplicate (4)

30. `test_a2_alpaca_lane.py::test_the_composition_registry_maps_alpaca_and_nothing_else` `:97-99` — the `PRODUCTION_PROVIDER_ADAPTERS == {}` half duplicates `test_import_isolation.py:99`. Delete those three lines; keep the composition assertions.
31. `test_routing_attempts.py::test_an_unmarked_attempt_may_still_settle` — restates the comment block at `test_routing_attempts.py:121-123` inside `test_dispatch_is_one_way_and_recorded_before_settlement`. Keep the standalone test, **delete the comment**; the comment is the duplicate.
32. `test_identity_and_errors.py::test_every_refusal_family_pins_reason_status_and_detail` — the uniqueness assertion (`:99-100`) does not depend on the `family` parameter and re-runs identically for all 20 parametrizations. Move it out into its own non-parametrized `test_no_two_refusal_families_share_a_reason`.
33. `test_identity_and_errors.py::test_status_codes_pin_the_retry_semantics` — asserts the status codes of 6 hand-picked families that `:87` already sweeps. Fold into the parametrized test as `assert family.status_code in RETRY_SEMANTICS[bucket_of(family)]`, or delete.

**Total: 33 named divergences** (issue estimated ~37; I found 33 I can defend with a line number, plus the 2 dead-canary fragments in item 18, plus the dead `served_context_refusals` knob).

---

## 3. Decisions for the owner

| Decision | Recommendation | Reason |
|---|---|---|
| **(G)** Is the fake-provider conformance suite evidence for PRD 15's fake-provider gate, or a smoke test? | **Evidence.** | It is the only thing in the repo that can exercise the multi-provider boundary before a second real broker exists — and `docs/broker-clerk-fleet-authority.md` already lists "a real additional broker" as a trigger that flips this latent risk live, which only makes sense if the fakes are the standing proof. |
| If evidence, add a `CONFIGURATION_ACCESS` operation | **Yes — to `fake_beta` only.** | `OperationReadiness.CONFIGURATION_ACCESS` is today exercised only as a *negative* catalog case (`test_operation_catalog.py:203-219`) and through the production Alpaca catalog; no fake declares one, so the readiness split in `resolve_route` is never proved across the provider boundary. |
| Split into how many PRs? | **Three.** | (1) fake divergence + everything it breaks — it is one semantic change with a blast radius and must land atomically; (2) the three flake fixes — independently mergeable, independently verifiable, and the only part CI flakiness cares about; (3) the rename/strengthen sweep — large, mechanical, zero behavioural risk, and reviewable as a diff of names. |
| Should any of this block #2081 or #2078? | **No.** | This lane touches only `tests/broker/fleet/**`. #2081 touches `restart.sh`, `app/main.py`, `fleet_boot.py`, `market_liveness.py`, `agent_identity.py`; #2078 touches `docs/**`. Zero overlap. |

---

## 4. Task breakdown (tests-only)

Common run prefix: `cd /Users/inkant/learn-ai/PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest …`. Tests under `tests/broker/fleet` that bind sockets need `dangerouslyDisableSandbox: true`.

### PR 1 — "The fakes must differ" (B, G)

#### Task 1 — Give the two fakes different canonicalization

**Files:** `PythonDataService/tests/broker/fleet/conftest.py`

Replace the lambda default at `:127` with two named module functions (also removes any E731 exposure):

```python
def _alpha_canonical_account_id(raw: str) -> str:
    """Alpha's canonical key: strip and upper-case."""
    return raw.strip().upper()


def _beta_canonical_account_id(raw: str) -> str:
    """Beta's canonical key: strip, lower-case, and fold ``-`` to ``_``.

    Deliberately different from alpha's in *shape* as well as case, so no
    case-insensitive comparison can collapse the two back together. A blank
    input still canonicalizes to the empty identity the service refuses
    (``app/broker/fleet/service.py:726``), so the empty-canonical gate stays
    reachable for both providers.
    """
    return raw.strip().lower().replace("-", "_")
```

**Shipped differs from this plan.** The plan's instruction below was to give
`FakeProviderAdapter.canonical_rule` a default of `_alpha_canonical_account_id`
(keeping the class constructible bare, alpha-shaped, with `fake_beta()`
overriding it). Review of the shipped PR (#2085) reversed this: a bare
`FakeProviderAdapter("fake_beta")` in `test_provider_conformance.py` was
silently inheriting alpha's rule, defeating the whole point of divergent
canonicalization. Shipped, `canonical_rule: Callable[[str], str]` is a
**required** field with no default — `FakeProviderAdapter` no longer has
alpha-shaped defaults, so `fake_alpha()` / `fake_beta()` are the only
constructors and that bug class cannot recur. Do not re-add a default.

In `FakeProviderAdapter` (`:119-155`): `canonical_rule: Callable[[str], str]` (required, no default).
In `fake_beta()` (`:167-173`): add `canonical_rule=_beta_canonical_account_id`.
Export both names in `__all__` (`:272-282`).

Raw `"acct-1"` now canonicalizes to `"ACCT-1"` under alpha and `"acct_1"` under beta.

**Run:** `pytest tests/broker/fleet -q`
**Before:** 222 passed. **After T1 alone:** 221 passed, **1 failed** — `test_assignments.py::test_identical_raw_account_ids_coexist_across_providers` at `:55` (`assert 'ACCT-1' == 'acct_1'`). That failure is the whole point of the task.

**Commit:** `test(fleet): make the two conformance fakes canonicalize differently`

#### Task 2 — Fix the test the collapse was hiding

**Files:** `PythonDataService/tests/broker/fleet/test_assignments.py`

Replace `:50-57`:

```python
    """Assignment identity is provider-qualified: one raw string, two providers,
    two *different* canonical keys and two independent reservations."""
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="x", tmp_path=control_dir.parent)
    beta = provision_lane(fleet_service, broker="fake_beta", label="y", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", alpha.clerk_id, "acct-1")
    second = _reserved(fleet_service, "fake_beta", beta.clerk_id, "acct-1")
    # Each provider owns its own canonicalization; the registry never folds
    # two providers' account identities into one key.
    assert first.canonical_external_account_id == "ACCT-1"
    assert second.canonical_external_account_id == "acct_1"
    assert first.canonical_external_account_id != second.canonical_external_account_id
    assert first.broker != second.broker
    # Neither reservation is visible under the other provider's key.
    assert fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="acct_1"
    ) is None
    assert fleet_service._store.read_assignment(
        broker="fake_beta", canonical_account_id="ACCT-1"
    ) is None
    assert fleet_service._store.list_active_assignments() == [first, second]
```

The final ordering assertion still holds: `store.py:582` orders `broker ASC, canonical_external_account_id ASC`, and `fake_alpha` < `fake_beta`.

**Run:** `pytest tests/broker/fleet/test_assignments.py -q`
**Before T1:** the two `is None` assertions fail (both keys resolve, because both providers produce `ACCT-1`). **After T1+T2:** 10 passed.

**Commit:** `test(fleet): assert provider-qualified account keys differ, not collapse`

#### Task 3 — Everything else the divergence touches (the full blast radius)

I traced every `fake_beta` reference outside `conftest.py` (38 lines across 9 files). Exactly these need updating:

| File:line | Today | Action |
|---|---|---|
| `test_assignments.py:55` | asserts equality | **T2** — the valuable find |
| `test_provider_conformance.py:236` | `FakeProviderAdapter("fake_beta")` bare — silently gets *alpha's* rule | replace with `fake_beta()`; import it at `:228` |
| `test_provider_conformance.py:188-208` | comment claims "provider-qualified keys differ" with only the broker column differing | add `beta_reservation = fleet_service.reserve_assignment(...)`; `assert beta_reservation.canonical_external_account_id == "acct_dup"`; assert `read_assignment(broker="fake_beta", canonical_account_id="ACCT-DUP") is None` |
| `test_operation_catalog.py:111-118` | validates both catalogs | add: `assert fake_alpha().canonical_account_id("acct-1") != fake_beta().canonical_account_id("acct-1")` — the guard that stops the collapse returning |
| `test_provider_conformance.py:133-175`, `:30-79`; `test_directory_and_aggregation.py:58/91/189`; `test_provisioning_and_volume.py:30/97/130/145`; `test_sessions_and_routing.py:114`; `test_reviewer_fences.py:162-170`; `test_registry_store.py:84` | reserve/route under `fake_beta` but never assert a canonical literal | **no change needed** — verified by reading each site |

Also add, to `test_provider_conformance.py`:

```python
def test_the_two_fakes_canonicalize_the_same_raw_account_differently() -> None:
    """The extension boundary is only provable when the fakes disagree.

    Two adapters that canonicalize identically cannot distinguish a
    provider-qualified key from a globally unique one — the exact bug
    provider-qualified assignment exists to prevent.
    """
    from tests.broker.fleet.conftest import fake_alpha, fake_beta

    raw = "  Acct-XYZ "
    assert fake_alpha().canonical_account_id(raw) == "ACCT-XYZ"
    assert fake_beta().canonical_account_id(raw) == "acct_xyz"
    # Not merely case: a casefold cannot collapse them back together.
    assert (
        fake_alpha().canonical_account_id(raw).casefold()
        != fake_beta().canonical_account_id(raw).casefold()
    )
    # Both still refuse the empty identity, so the service's gate stays reachable.
    assert fake_alpha().canonical_account_id("   ") == ""
    assert fake_beta().canonical_account_id("   ") == ""
```

**Run:** `pytest tests/broker/fleet -q` (unsandboxed)
**Before:** 222 passed / 0 failed. **After PR 1:** **224 passed, 0 failed** (222 + the new guard test + the T3 conformance assertion is inline, so +1 function; count the new `test_the_two_fakes_canonicalize…` and the split added in T3b if taken).

**Commit:** `test(fleet): update every fake_beta site for the diverged canonicalization`

#### Task 3b — `CONFIGURATION_ACCESS` operation on `fake_beta` (G)

**Files:** `PythonDataService/tests/broker/fleet/conftest.py`, `test_operation_catalog.py`, `test_provider_conformance.py`

Add to `_FAKE_BETA_OPERATIONS` (`conftest.py:77-100`) and to `FAKE_BETA_CAPABILITIES` (`:36-41`):

```python
        ProviderOperation(
            operation_id="configuration_apply",
            method="POST",
            path_template="/configuration/apply",
            agent_path_template="/api/fake-beta/configuration/apply",
            capability=Capability.CONFIGURATION_MANAGE,
            readiness=OperationReadiness.CONFIGURATION_ACCESS,
            requires_effective_account=False,
            idempotency=OperationIdempotency.ONE_SHOT,
        ),
```

(`requires_effective_account=False` is mandatory — `validate_operation_catalog` refuses the combination, per `test_operation_catalog.py:203-219`.)

Then, in `test_provider_conformance.py`:

```python
def test_a_configuration_operation_routes_on_an_unbound_lane_of_the_declaring_provider(
    control_dir: Path, fleet_service
) -> None:
    """Readiness is per-provider: only beta declares a configuration-access
    operation, and it stays routable before any binding is confirmed."""
    from app.broker.fleet.provider import Capability, OperationReadiness

    beta = provision_lane(fleet_service, broker="fake_beta", label="cfg", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=beta.clerk_id, worker_key=beta.worker_key
    )
    fleet_service.require_capability(
        broker="fake_beta", capability=Capability.CONFIGURATION_MANAGE
    )
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(
            broker="fake_alpha", capability=Capability.CONFIGURATION_MANAGE
        )
    # Unbound, so execution refuses…
    with pytest.raises(ClerkUnreachable):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=beta.clerk_id)
    # …but the configuration surface stays reachable (the repair path).
    _clerk, session, assignment = fleet_service.resolve_route(
        broker="fake_beta",
        clerk_id=beta.clerk_id,
        readiness=OperationReadiness.CONFIGURATION_ACCESS,
    )
    assert assignment is None
    assert session is not None
```

**Run:** `pytest tests/broker/fleet/test_provider_conformance.py tests/broker/fleet/test_operation_catalog.py -q`
**Before:** the new test does not exist; adding it *before* the conftest change fails at `require_capability(broker="fake_beta", capability=Capability.CONFIGURATION_MANAGE)` with `BrokerClerkCapabilityUnavailable`. **After:** 16 passed.

Note: `test_directory_and_aggregation.py:78-80` asserts beta's directory capabilities equal `sorted(FAKE_BETA_CAPABILITIES)` — it reads the constant, so it follows automatically. Verified.

**Commit:** `test(fleet): give fake_beta a CONFIGURATION_ACCESS operation (PRD 15 gate)`

---

### PR 2 — Three latent flakes (F)

#### Task 4 — Pin the CLI retirement test's clock

**Files:** `PythonDataService/tests/broker/fleet/test_fleet_cli.py`

Replace the three floating `now_ms_utc()` calls (`:256`, `:299`, `:338`) and the `+ 1` rewrite (`:297`) with one anchor and fixed offsets:

```python
    # One anchor, explicit offsets: nothing in this test depends on wall-clock
    # milliseconds elapsing between two CLI invocations. The evidence-age and
    # window bounds are 24h / 7d (compatibility_retirement.py:28-29), so these
    # offsets are inside every limit while still ordering strictly.
    anchor_ms = now_ms_utc()
    window_start_ms = anchor_ms - 3_000
    window_end_ms = anchor_ms - 2_000
    evidence_ms = anchor_ms - 1_000
    aggregate = {"schema_version": 2, "updated_at_ms": window_start_ms, "route_hits": []}
```

After both `compatibility-snapshot` calls, rewrite **both** payloads (not just the end one):

```python
    start_payload = json.loads(start_snapshot.read_text(encoding="utf-8"))
    end_payload = json.loads(end_snapshot.read_text(encoding="utf-8"))
    start_payload["captured_at_ms"] = window_start_ms
    end_payload["captured_at_ms"] = window_end_ms
    start_snapshot.write_text(json.dumps(start_payload), encoding="utf-8")
    end_snapshot.write_text(json.dumps(end_payload), encoding="utf-8")
```

and use `evidence_ms` for `generated_at_ms`, `observed_at_ms` and `issued_at_ms`.

Invariant restored: `receipt_issued_at_ms (anchor-1000) >= window_end_ms (anchor-2000)` by construction — `compatibility_retirement.py:148` can no longer fire on a fast machine. `test_compatibility_retirement.py:51-62` already uses this exact pattern (`captured_at_ms=1_000` / `2_000`); this makes the CLI test consistent with it.

**Run:** `pytest tests/broker/fleet/test_fleet_cli.py -q`
**Before:** 5 passed (today, by luck of the clock). **After:** 5 passed, deterministically.

**Commit:** `test(fleet): pin the compatibility-retirement CLI test to an explicit clock`

#### Task 5 — Separate must-timeout from must-succeed budgets

**Files:** `PythonDataService/tests/broker/fleet/test_lane_runtime.py`

At `:387-407`, the injected sleep is `0.2` and the success budget is `< 0.1`; a 100 ms scheduling hiccup on a loaded CI box fails it. Widen the gap rather than tightening the assert — separate constants, one for each role:

```python
#: How long the injected slow export blocks. Must-timeout side of the budget.
_SLOW_EXPORT_SECONDS = 2.0
#: The response must return well inside this. Must-succeed side of the budget.
#: The two are an order of magnitude apart on purpose: one small shared
#: constant makes a loaded runner fail a test that is not about timing.
_RESPONSE_BUDGET_SECONDS = 0.5
```

and use them at `:395` (`time.sleep(_SLOW_EXPORT_SECONDS)`) and `:406` (`assert time.monotonic() - started_at < _RESPONSE_BUDGET_SECONDS`). The property under test — "the export runs off-loop" — is unchanged; the margin goes from 2× to 4× and the absolute floor from 100 ms to 500 ms.

**Run:** `pytest tests/broker/fleet/test_lane_runtime.py -q`
**Before:** 18 passed (flaky under load). **After:** 18 passed; wall time rises by ~1.8 s in that one test, well inside the 120 s gate.

**Commit:** `test(fleet): separate the must-timeout and must-succeed budgets in the lane-runtime export test`

#### Task 6 — Scope the `os` monkeypatches to the module under test

**Files:** `PythonDataService/tests/broker/fleet/test_lane_runtime.py`

`monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", …)` resolves `os` to the global module object, so `:368` and `:562` mutate `os.replace` / `os.fsync` **process-wide**. `test_parent_directory_fsync_failure…` (`:547`) counts `fsync` calls and raises on the second — any concurrent `os.fsync` in the process shifts the count.

Replace the module's `os` *binding* instead. `lane_runtime` uses exactly `os.fsync`, `os.replace`, `os.open`, `os.O_RDONLY`, `os.close` (verified), so a complete shim is five names. Add one helper to the file:

```python
def _scoped_os(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Swap the ``os`` *binding* inside ``lane_runtime`` only.

    ``monkeypatch.setattr("app.broker.fleet.lane_runtime.os.replace", …)``
    reads through to the process-global ``os`` module and mutates it for
    every thread and every other test in the session. Rebinding the module
    attribute cannot leak: only ``lane_runtime``'s own lookups see the shim.
    """
    from app.broker.fleet import lane_runtime

    shim = SimpleNamespace(
        fsync=os.fsync, replace=os.replace, open=os.open, close=os.close, O_RDONLY=os.O_RDONLY
    )
    for name, value in overrides.items():
        setattr(shim, name, value)
    monkeypatch.setattr(lane_runtime, "os", shim)
```

Then `:367-368` becomes `_scoped_os(monkeypatch, replace=refuse_replace)` and the restore at `:382` becomes `_scoped_os(monkeypatch)`; `:562` becomes `_scoped_os(monkeypatch, fsync=refuse_parent_sync)` and `:571` becomes `_scoped_os(monkeypatch)`. The local `original_replace` (`:367`) and `original_fsync` (`:552`) stay — `refuse_parent_sync` still calls `original_fsync` for the first call.

**Run:** `pytest tests/broker/fleet/test_lane_runtime.py -q` then `pytest tests/broker/fleet -q -p no:randomly` (both unsandboxed)
**Before:** 18 passed / 222 passed, with `os.replace` globally swapped for the duration of two tests. **After:** identical counts, with the swap confined to one module object.

**Commit:** `test(fleet): scope the lane-runtime os monkeypatches to the module under test`

---

### PR 3 — Falsifiability and the rename sweep

#### Task 7 — Make the B-scoped agent's identity agent-derived (C)

**Files:** `PythonDataService/tests/broker/fleet/test_b_scoped_contracts.py`

Today `_Fleet.__init__` sets `self.identity["routing_epoch"] = session.routing_epoch` (`:271`) from the registry object the coordinator also reads — one source, so the echo tautologically matches. A real agent takes its epoch from the **response to its own registration** (`app/broker/fleet/presence.py:363-366`: `SessionInfo(routing_epoch=int(body["routing_epoch"]))`), which is what `fleet_boot.py:267` then serves.

Change `_Lane.register_and_confirm` to return the registration **response body** rather than the record, and have `_Fleet` build its identity from that body alone:

```python
    def register_and_confirm(self) -> dict[str, object]:
        """Register as an agent would and return the *response* it receives.

        The agent never reads the registry; it knows only what the
        registration answered. Building the served identity from this body —
        not from the ``ClerkSessionRecord`` the coordinator also reads —
        is what lets ``verify_identity_echo`` fail when the coordinator pins
        a different epoch than the lane serves.
        """
        session = self.service.register_agent_session(...)
        registration_response = {
            "agent_instance_id": session.agent_instance_id,
            "routing_epoch": session.routing_epoch,
        }
        ...
        return registration_response
```

and add the missing negative test — the one that proves the echo check is live end to end:

```python
async def test_a_lane_serving_a_different_epoch_fails_the_coordinators_echo_check(
    tmp_path: Path,
) -> None:
    """FR-076: the echo is a check, not a formality.

    Reads pass the agent-side pin gate (``_pin_mismatch`` compares epoch only
    for mutations), so a lane serving a stale epoch answers 200 — and the
    coordinator's ``verify_identity_echo`` is the only thing standing
    between that answer and the caller.
    """
    fleet = _Fleet(tmp_path)
    try:
        # The lane re-registered and moved on; the coordinator still pins the
        # epoch its registry recorded.
        fleet.identity["routing_epoch"] = int(fleet.identity["routing_epoch"]) + 1
        async with fleet.client() as client:
            response = await client.get(f"{fleet.base}/account")
        assert response.status_code == 409
        assert response.json()["reason"] == "clerk_identity_mismatch"
    finally:
        fleet.stop()
```

**Run:** `pytest tests/broker/fleet/test_b_scoped_contracts.py -q` (unsandboxed)
**Before:** the new test does not exist; written against today's fixture it still passes only because the mutation is applied by hand — that is the point, it *establishes* the path is live. **After T7:** 25 passed.

#### Task 8 — Make the provider receipt agent-minted (C)

**Files:** `PythonDataService/tests/broker/fleet/test_b_scoped_contracts.py`

`_build_agent_app` mints `f"provider/command-{body.get('idempotency_key')}"` (`:140`), so `assert receipt.upstream_receipt_ref == "provider/command-dk-42"` (`:482`) proves only that the coordinator's own string round-tripped. Give the agent its own ledger:

```python
def _build_agent_app(
    identity: dict[str, object],
    *,
    flip_identity_after_first_event: bool = False,
    minted_receipts: list[str] | None = None,
) -> FastAPI:
    ...
    ledger = minted_receipts if minted_receipts is not None else []

    @agent.post(".../actions")
    async def action(account_id: str, sid: str, request: Request) -> JSONResponse:
        body = await request.json()
        # A real provider's durable receipt is minted by the provider. Deriving
        # it from the caller's idempotency key would make the coordinator's
        # "carried, never fabricated" contract unfalsifiable.
        receipt_id = f"provider/receipt-{len(ledger) + 1:04d}-{uuid.uuid4().hex[:8]}"
        ledger.append(receipt_id)
        return JSONResponse({..., "receipt_id": receipt_id})
```

`_Fleet.__init__` owns `self.minted_receipts: list[str] = []` and passes it through. Then at `:472`/`:482`:

```python
        assert delivered.json()["receipt_id"] == fleet.minted_receipts[-1]
...
    assert receipt.upstream_receipt_ref == fleet.minted_receipts[-1]
    assert "dk-42" not in receipt.upstream_receipt_ref
```

and at `:924`: `assert fleet.minted_receipts[-1] in body["message"]`.

**Run:** `pytest tests/broker/fleet/test_b_scoped_contracts.py -q -k "routing_receipt or redispatch"` (unsandboxed)
**Before T8:** `assert "dk-42" not in receipt.upstream_receipt_ref` **FAILS** — the stored ref is literally `provider/command-dk-42`. **After:** 2 passed.

**Commit (T7+T8):** `test(fleet): derive the B-scoped agent's identity and receipts from the agent`

#### Task 9 — Make the unapproved-endpoint refusal real (D)

**Files:** `PythonDataService/tests/broker/fleet/test_b_scoped_contracts.py`

Replace `_coordinator_app`'s stub (`:232-239`) with the production resolver:

```python
    from app.broker.fleet.routing import coordinator_delivery_for
    coordinator.state.fleet_lane_router = LaneRouter(
        service=lane.service,
        # The production resolver: it reads the approved-endpoint row and the
        # clerk's coordinator token, and refuses before any socket is opened.
        delivery_for=coordinator_delivery_for(
            lane.service, {lane.clerk_id: COORDINATOR_TOKEN}
        ),
    )
```

`agent_base_url` is no longer needed: the happy path resolves through the row `lane.approve(self.agent.base_url)` wrote at `:269`. Then rewrite `test_commands_without_an_approved_endpoint_refuse` (`:546-586`):

```python
        coordinator = _RealServer(_coordinator_app(lane))
        ...
                assert response.status_code == 503
                assert response.json()["reason"] == "clerk_unreachable"
                # The refusal names the *approval ceremony*, which a transport
                # failure could never produce. This is the assertion that
                # distinguishes the gate from a connection refused.
                assert "no approved endpoint row" in response.json()["message"]
```

and add the second, currently untested branch of `routing.py:645` — a session citing an endpoint ref that an approved row does not match:

```python
async def test_a_session_citing_a_different_endpoint_than_the_approved_row_refuses(
    tmp_path: Path,
) -> None:
    """The approval is per-reference: an approved row for ``agent:paper-1``
    does not authorize a session that cites ``agent:paper-2``."""
```

**Run:** `pytest tests/broker/fleet/test_b_scoped_contracts.py -q` (unsandboxed)
**Before T9:** the `"no approved endpoint row"` assertion **FAILS** — today's message is `"Clerk … could not serve account_read; the transport detail is in the coordinator log."` **After:** 26 passed, and the `http://127.0.0.1:9` sentinel is gone from the file entirely.

**Commit:** `test(fleet): refuse an unapproved endpoint through the real resolver, not a closed port`

#### Task 10 — Sweep all eight secret sinks, or delete the canary (E)

**Files:** `PythonDataService/tests/broker/fleet/test_secret_absence.py`

The eight durable/observable sinks of one full ceremony:

| # | Sink | Today |
|---|---|---|
| 1 | registry SQLite main file (`registry_database_path`) | **swept** (`:93`) |
| 2 | registry `-wal` / `-shm` | **swept** (`:94-99`) |
| 3 | `directory(include_retired=True)` payload | **swept** (`:100`, `:107`) |
| 4 | `caplog` at DEBUG | **swept** (`:101-103`, `:108`) |
| 5 | volume marker `<volume_root>/.learn-ai-clerk-volume.json` (`app/broker/fleet/volume.py:32`) | **not swept** |
| 6 | confirmation evidence `<volume_root>/confirmation.json` (`app/broker/fleet/confirmation.py:33`) | **not swept** |
| 7 | routing-receipt projection — `list_routing_receipts()` incl. `upstream_receipt_ref` and `nonsecret_target_ref` | **not swept** |
| 8 | backup artifacts — `create_registry_backup` output + `fleet-registry-manifest.json` (`recovery.py:53`) | **not swept** |

Sinks 5–8 are the ones that *could* carry an operator-supplied string; 1–4 are already covered.

Do **both** halves:

- **Delete the env canary.** `FAKE_ALPHA_API_KEY_ID` / `FAKE_ALPHA_API_SECRET_KEY` (`:22-24`, `:28-32`, `:109-112`) can reach no sink because no fleet module reads `os.environ` (verified). Remove the `seeded_env` fixture and the `CREDENTIAL_VARIABLE_NAMES` loop, with the reason in the module docstring: *"An environment-variable canary is not a test: `app/broker/fleet/**` reads no environment variable, so the assertion can only ever pass. What is worth pinning is the negative space around values the ceremony genuinely handles."*
- **Replace it with a falsifiable canary.** Drive the ceremony with a distinctive operator-supplied string that the ceremony really does touch — the display label and the `nonsecret_target_ref` — and sweep all eight sinks for the *worker key* and the two *service tokens* (values the ceremony provably mints), extending `_drive_ceremonies` to return the four extra artifacts:

```python
    sinks = {
        "registry": registry_bytes,
        "directory": repr(payload).encode("utf-8"),
        "logs": logged.encode("utf-8"),
        "volume_marker": marker_path(lane.volume_root).read_bytes(),
        "confirmation_evidence": (lane.volume_root / EVIDENCE_FILENAME).read_bytes(),
        "routing_receipts": repr(receipts).encode("utf-8"),
        "backup_database": (backup_dir / ...).read_bytes(),
        "backup_manifest": (backup_dir / BACKUP_MANIFEST_FILENAME).read_bytes(),
    }
    for name, blob in sinks.items():
        if name == "registry":
            continue  # the worker key is durable registry identity, by design
        assert lane.worker_key.encode("utf-8") not in blob, name
        assert b"svct_" not in blob, name
```

**Run:** `pytest tests/broker/fleet/test_secret_absence.py -q`
**Before:** 3 passed (2 of them vacuously). **After:** 3 passed, all falsifiable. Prove it once by hand: temporarily write the worker key into the confirmation evidence and confirm the sweep fails naming `confirmation_evidence`.

**Commit:** `test(fleet): sweep all eight secret sinks; delete the unfalsifiable env canary`

#### Task 11 — The rename/strengthen sweep

**Files:** the 12 files named in §2. One commit per group so the reviewer can read them separately:

- `test(fleet): rename twelve tests whose names outran their assertions` (A1 items 1–12)
- `test(fleet): strengthen fourteen assertions to cover the behaviour their names claim` (A2 items 16–29, excluding 13/14/15 which land in PR 1 / T9 / T10)
- `test(fleet): fold four duplicated assertions into one owner each` (A3 items 30–33)

**Run:** `pytest tests/broker/fleet tests/contracts -q` (unsandboxed) plus `ruff check tests/broker/fleet/`
**Before:** 315 passed. **After:** 315 + net new tests (2 from A3 splits, 1 guard, 1 CONFIGURATION_ACCESS, 2 from T7/T9) = **321 passed, 0 failed**, ruff clean.

---

## 5. Grouping advice

**No production file needs to change for this lane.** Confirmed per task:

- B/G — the fakes and the catalog are entirely inside `tests/broker/fleet/conftest.py`.
- C — the agent app and `_Fleet` are test fixtures; `agent_identity.py` and `delivery.py` are exercised as-is.
- D — `coordinator_delivery_for` already exists in `app/broker/fleet/routing.py:625`; the test simply starts calling it.
- E — the sinks are all readable from the test.
- F — all three fixes are inside `test_fleet_cli.py` / `test_lane_runtime.py`.

**One caveat, explicit:** divergence #5 (`test_compatibility_evidence_separates_unauthorized_and_not_found_responses`) can be *renamed* here, but genuinely separating 401/403 from 404 would need `response_class` in `app/broker/fleet/lane_runtime.py` to widen — and that changes the retirement evidence schema (`RETIRED_COMPATIBILITY_RESPONSE_CLASSES`, `compatibility_retirement.py:39`), the persisted `route_hits.json` on every lane, and the decision receipt. **That is a separate production issue, not this lane.** Recommend renaming now and filing the schema question.

**PR boundaries:**

| PR | Scope | Depends on | Parallel-safe with |
|---|---|---|---|
| 1 | T1–T3b — fake divergence, blast radius, `CONFIGURATION_ACCESS` | nothing | #2081, #2078, PRs 2 and 3 |
| 2 | T4–T6 — the three flakes | nothing | everything |
| 3 | T7–T11 — falsifiability + rename sweep | nothing (T7–T9 touch `test_b_scoped_contracts.py`, which PR 1 does not) | everything |

Only file-level collision: PR 1 and PR 3 both touch `test_provider_conformance.py` and `test_operation_catalog.py`. Land PR 1 first and rebase PR 3, or keep the A1 renames for those two files in PR 1.

---

## 6. Risks the issue text missed

1. **`api_key` and `endpoint_url` are dead canaries** in `test_registry_contains_no_custody.py:41,44` — `_identifier_tokens` splits on `[a-z0-9]+`, so no underscore-bearing fragment can ever match. Same failure mode as the env canary, in the suite's other secret sweep, and the issue did not name it.
2. **`served_context_refusals` is a dead fixture knob.** `conftest.py:129,151-155` implements a refusal hook that **no test sets** and **no production code calls** — `validate_served_context` has zero callers outside `test_a2_alpaca_lane.py:71`. PR #2078's doc ranks wiring-or-deleting it #4. If the fake-provider suite is evidence (decision G), this is a hole in the evidence, not just dead code.
3. **`PRODUCTION_PROVIDER_ADAPTERS` is a stale gate pinned by two tests.** `provider.py:297` is an empty mapping whose only live consumer is a `service.py:160` fallback. Two tests assert it stays empty "until Phase 2" — a phase that closed. This is the suite pinning the *absence* of something rather than the presence of the real thing (`fleet_composition.production_provider_adapters()`), which is how a registry could silently go empty without a red test.
4. **`test_a2_alpaca_lane.py::test_a_wrong_identity_echo_is_an_uncertain_outcome` starts a real uvicorn server it never uses** (`agent_server` fixture at `:514`). Not just a naming issue — it is per-run wall time and a port bind in a suite the sandbox already struggles with.
5. **`_InternalServer.disconnect_seen` is asserted by no test** (`test_internal_http.py:45`, set at `:96`), and `/events` does `set()` immediately followed by `await …wait()` on the same event (`:71-74`) — a no-op masquerading as a hold. The "cancellation disconnects the server" property is written into the fixture and then never checked.
6. **The `_ALLOWED_TABLES` allowlist and the store's own table check disagree.** `test_registry_contains_no_custody.py:18-27` lists 8 tables; `test_registry_store.py:30-37` asserts a 6-table **subset** (`<=`). If a new table lands, the subset check passes and only the allowlist catches it — but the allowlist test builds the schema from `schema.apply_schema` on a fresh file, so it would *also* pass after someone adds the new name to the list. There is no single place that says "a new registry table needs a reviewer".
7. **`test_compatibility_retirement.py:132` is a third unfalsifiable canary** — `for forbidden in ("account", "clrk_", "token", "secret", "url")` over a receipt assembled from a closed literal dict (`compatibility_retirement.py:158-174`). None of those strings can reach it by any code path.
8. **The suite's only genuine cross-connection race is one test.** `test_assignments.py:276` is the sole place two `FleetRegistryStore.open` connections contend; everything else labelled "race"/"concurrent" either shares a service or is a deterministic interleave. The DDL (composite PK, partial UNIQUE indexes, the RAISE(ABORT) triggers) is what actually holds the line — and after the renames in A1, the suite will *say so*, which is a strictly better position than a suite that reads as if it proved contention it never exercised.

### Critical Files for Implementation

- `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/conftest.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/test_b_scoped_contracts.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/test_lane_runtime.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/test_assignments.py`
- `/Users/inkant/learn-ai/PythonDataService/tests/broker/fleet/test_provider_conformance.py`
