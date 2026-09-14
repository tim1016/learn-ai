# Fleet E multi-provider conformance and operator acceptance

**Status:** Delivery E ceremony and acceptance template. This document does
not record operator acceptance, deployment qualification, or operational
rollout. The controlling decision remains [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md);
the provider-scale gate is the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md)
§14/§15, and Delivery D's isolated-role record remains required under the
[two-Clerk rollout runbook](fleet-d-two-clerk-rollout.md).

## What the bounded ceremony proves

From `PythonDataService`, write the nonsecret JSON result to an
access-controlled location outside the source checkout:

```bash
.venv/bin/python -m scripts.run_broker_fleet_conformance \
  --evidence-path <restricted-evidence>/fleet-e-conformance.json
```

The command runs only the focused tests named in its JSON record, with a
maximum 90-second test bound. It reuses `fake_alpha` and `fake_beta` plus the
existing N-clerk fixtures. The code checks cover:

- provider and Clerk isolation;
- undeclared capability and mixed protocol/older-adapter-version refusal;
- a failed lane while the correctly identified survivor remains usable, with
  partial aggregation retaining each lane rather than omitting or substituting
  one;
- identity on every stream event and stale-event closure;
- `outcome_unknown` reconciliation by the original command identity, with no
  automatic resubmission; and
- the absence of fake providers from production Compose composition, the
  production adapter composition, and the committed OpenAPI contract.

The generated document labels itself `code_conformance_only`. Its
`isolated_qualification`, `operator_sign_off`, and `production_rollout`
layers always remain `required`; no command-line option can change them to a
passing state.

## Evidence boundaries

| Evidence class | Required record | What it may establish | What it cannot establish |
|---|---|---|---|
| Code checks | The generated Fleet E JSON and exact commit | Fake-provider conformance and source-contract boundary | Physical volumes, real credentials/upstreams, or an operator decision |
| Isolated qualification | Delivery D actual-role Compose JSON, redacted transcript, image/commit, and cleanup result | The qualifying fake-upstream roles and inspected isolation facts | Production mount/credential reality, real broker/feed behavior, or rollout readiness |
| Operator sign-off | Named operator, timestamp, reviewed evidence references, decision, and exceptions in a restricted record | That a human accepted the stated bounded conditions | A Live command authorization not separately recorded |
| Production rollout | Reviewed deployment qualification, Paper canary, Live read-only, compatibility-retirement decision, rollback evidence, and incident contacts | The recorded production stage only | A broader broker/provider authority or waived provider safety gates |

Do not place names, account IDs, opaque Clerk IDs, credentials, tokens, raw
URLs, raw logs, or acceptance records in Git. The source-checkout JSON is
nonsecret by design; the corresponding restricted record references it by
path, commit, and checksum.

## Operator acceptance checklist

The operator records every item below in the restricted acceptance record.
A blank, missing, or unreviewed item means acceptance is still required.

1. Attach the passing Fleet E code-conformance JSON, its commit, and the
   targeted-test transcript.
2. Attach the current Delivery D isolated actual-role Compose qualification
   record. Confirm it remains explicitly fake-upstream evidence, not
   production qualification.
3. Review the provider/clerk isolation, capability/version refusals, survivor
   behavior, per-event identity, and uncertainty/no-resubmit results by their
   named JSON checks.
4. Confirm that `fake_alpha` and `fake_beta` are test-only and that the
   production registry still enables only Alpaca. A future real provider needs
   its own accepted authority, provider-owned safety evidence, recovery
   design, and delivery; this checklist does not enroll one.
5. Review backup, restore, reassignment, registry recovery, and compatible
   independent single-Clerk rollback evidence. Stop on a shared root,
   assignment release, copied custody/arming evidence, reminted identity, or
   implicit target.
6. Make a separate written decision for the compatibility observation window
   and consumer inventory. Zero traffic in an idle window is not retirement
   proof.
7. Record the operator's decision, date/time in `int64 ms UTC`, open risks,
   rollback contact, and next eligible rollout stage. This is an acceptance
   decision, not a test output.

## Live gates remain unchanged

This ceremony neither arms Live nor sends a Live command. A separately named
authorizer must still approve each eligible Live command after the unchanged
provider-owned envelope, host-only arming, custody, capability, exact target,
idempotency, risk, and reconciliation gates pass. An `outcome_unknown`
requires read-only reconciliation using the original command identity; it
never permits automatic resubmission or a replacement-Clerk command.
