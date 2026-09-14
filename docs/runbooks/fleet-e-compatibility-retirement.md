# Fleet E compatibility-read retirement

**Status:** Delivery E host-only operator procedure. This runbook retires only
the retained browser-direct, unscoped `GET`/`HEAD` read aliases observed by
Delivery D. It does not retire canonical broker-and-Clerk routes, broaden a
fleet mutation, alter provider authority, or authorize a Live action.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md),
the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md),
and the [Delivery D ownership matrix](../design/fleet-d-runtime-ownership-matrix.md).
Enrollment, retirement of a Clerk, restore, reassignment, endpoint changes, and
Live arming remain their own host ceremonies.

## Non-negotiable retirement gate

The normal state is `measurement`: retained unscoped reads stay available and
the Delivery D collector writes its compact route-family aggregate. Retire an
alias only when one bounded representative window has all of the following:

- a start and end snapshot for every aggregate source, with matching labels;
- zero aggregate delta for every retained compatibility route family and
  response class throughout that window;
- a complete, named consumer inventory where the attestations collectively
  name every retained route family;
- a fresh scoped-route health artifact reporting exactly zero unresolved
  scoped-route failures; and
- a fresh, explicit named-operator receipt that says both that the window was
  representative and that compatibility-read retirement is authorized.

Zero traffic is necessary but never sufficient. An idle deployment, a missing
consumer, a stale artifact, a decreasing aggregate, a mismatched source set, a
non-zero scoped-route failure, or a missing/ambiguous operator receipt refuses
retirement. Keep the aliases in `measurement`, investigate, and collect a new
window. Do not edit aggregate counts or state JSON to force a decision.

The aggregate and all receipt forms are privacy bounded: retain route family,
response class, count, and `int64 ms UTC` timestamps only. Do not place raw
URLs, query values, headers, account or Clerk IDs, credentials, request bodies,
or access logs in the rollout record.

## Prepare the restricted record

Create the restricted rollout directory with permissions appropriate for the
operator record. Use stable nonsecret source labels such as `combined`,
`paper`, and `live`; do not use account, Clerk, endpoint, or volume identities
as labels.

The named consumer inventory must be a JSON object of this form. Every retained
family must occur in at least one `route_families` list.

```json
{
  "schema_version": 1,
  "complete": true,
  "generated_at_ms": 1760000000000,
  "consumers": [
    {
      "consumer": "alpaca-desk",
      "attestation": "all-retained-reads-use-canonical-scoped-routes",
      "route_families": [
        "broker_bots",
        "broker_configuration",
        "broker_v2_panel",
        "brokers_lane_extras",
        "run_replay"
      ]
    }
  ]
}
```

The scoped-route health artifact is a current, bounded observation, not an
assertion inferred from no alias traffic:

```json
{
  "schema_version": 1,
  "observed_at_ms": 1760000000000,
  "unresolved_scoped_route_failures": 0
}
```

The explicit operator receipt must be dated after the end snapshot:

```json
{
  "schema_version": 1,
  "receipt_id": "fleet-e-acceptance-001",
  "operator": "named-fleet-operator",
  "issued_at_ms": 1760000000000,
  "representative_window": true,
  "retire_compatibility_reads": true
}
```

## Capture, evaluate, then retire

Run these commands on the host against the copied, compact aggregates. Capture
one start and one end snapshot for each source after the approved measurement
duration; preserve both snapshots in the restricted record.

```sh
cd PythonDataService
.venv/bin/python -m scripts.manage_broker_fleet compatibility-snapshot \
  --evidence-path /restricted-record/paper-route-hits.json \
  --snapshot-path /restricted-record/paper-start.json \
  --source-label paper

# After the approved representative window, capture paper-end.json and the
# corresponding start/end snapshots for every other source.

.venv/bin/python -m scripts.manage_broker_fleet compatibility-evaluate \
  --start-snapshot /restricted-record/paper-start.json \
  --end-snapshot /restricted-record/paper-end.json \
  --consumer-inventory /restricted-record/consumer-inventory.json \
  --scoped-route-evidence /restricted-record/scoped-route-health.json \
  --operator-receipt /restricted-record/operator-receipt.json \
  --decision-receipt-path /restricted-record/compatibility-decision.json
```

Repeat `--start-snapshot` and `--end-snapshot` for every source. Evaluation is
intentionally non-mutating: a successful result remains `measurement` and
writes the durable eligible decision receipt. A refusal exits with code 2 and
must leave every alias in measurement mode.

Only after reviewing that receipt, apply it to every serving Clerk root. The
state file is lane-local because the runtime must not obtain custody or a
cross-lane mount just to read a compatibility decision.

```sh
.venv/bin/python -m scripts.manage_broker_fleet compatibility-retire \
  --start-snapshot /restricted-record/paper-start.json \
  --end-snapshot /restricted-record/paper-end.json \
  --consumer-inventory /restricted-record/consumer-inventory.json \
  --scoped-route-evidence /restricted-record/scoped-route-health.json \
  --operator-receipt /restricted-record/operator-receipt.json \
  --decision-receipt-path /restricted-record/compatibility-decision.json \
  --route-state-path /mounted/paper/compatibility/route_state.json \
  --route-state-path /mounted/live/compatibility/route_state.json
```

The route-state writes are durable replacements. `retired` returns `410` only
for the fixed retained unscoped read aliases. Canonical scoped routes and every
mutation retain their existing routing and provider-owned gates. A missing
state file defaults to `measurement`; an existing corrupt or incomplete state
file refuses only the retained aliases with `503` and requires host repair. It
never silently re-enables an alias.

## Record and rollback limits

Attach the snapshot set, consumer inventory, scoped-route health observation,
operator receipt, evaluator decision, and applied lane-state paths to the
Delivery E record. Also attach the exercised backup/restore, reassignment,
registry-recovery, and compatible-rollback transcripts required by the
[Delivery D recovery handoff](fleet-d-recovery-and-rollback.md). This procedure
does not create those transcripts and does not upgrade D posture into
operational qualification by itself.

If an alias must be restored, stop and treat it as a compatibility incident:
review the canonical consumer failure and use the compatible rollback posture
from Delivery D. Do not remove the evidence receipt, mutate historical
aggregates, release assignments, switch enrolled volumes to `combined`, or
bypass Live arming, envelope, custody, risk, capability, binding, idempotency,
or outcome-reconciliation gates.
