# ADR-0025: Single dominant headline — notice placement as a function of tier × actionability

**Status**: Accepted 2026-07-08. Decided in the 2026-07-08 grilling session ("if the operator can take no corrective measure from the cockpit, what use is the message?").
**Related:** ADR-0015 § Amendment 2026-07-08 (honest actionability and mandatory resolution — the axes this ADR's placement function consumes), ADR-0013 (operator-surface boundary), operator-observability taxonomy memo 2026-07-04 (this ADR closes its open placement/severity question), PRD #951 (stream-primary layout — lower documentation section).

## Context

The bot control page grew four independent banner sources — the
control-plane banner, the broker-evidence banner, the runtime-freshness
headline, and the incident headline — plus the host-process notice, the
attention dropdown, the blockage ladder, the trader-guidance pane, and
the incidents panel. There is no arbiter across them. Each surface
honestly renders its own headline; the sum is a wall of simultaneous
alerts in which the operator cannot find the one fact that matters.
Field observation (2026-07-08): "so many error messages populate the
screen, but the user is not able to take a corrective measure."

Two stacked red banners do not communicate two problems; they
communicate panic. Alarm fatigue is trained, not innate — every
non-dominant banner teaches the operator to stop reading banners.

ADR-0015's 2026-07-08 amendment gave every notice two orthogonal,
honest axes: **tier** (trust impact only) and **actionability**
(`actuatable | routed | self_resolving | no_remedy`), plus a mandatory
resolution statement. Those axes make a deterministic placement
function possible for the first time.

## Decision

### 1. The placement function

Placement is a pure function of `tier × actionability`. No surface
opts out; no notice chooses its own placement.

| tier × actionability | Renders where |
|---|---|
| `critical` × anything | The single banner slot — one arbitrated winner across all banner sources. Other concurrent criticals fold behind a "+N more" affordance that opens the ladder. `no_remedy` criticals lead with the trust impact and the resolution statement. |
| `warning` × `actuatable` / `routed` | Attention dropdown row with the affordance inline. Never a banner. |
| `warning` × `self_resolving` / `no_remedy` | Attention dropdown, quiet (no pulse). Resolution statement visible. |
| `info` × anything | Quiet status region (session card and siblings). Never a banner, never the attention dropdown. |

### 2. At most one banner, ever

The page renders **at most one banner at any time**. Arbitration across
all banner sources (control-plane, broker-evidence, runtime-freshness,
incident, and any future source):

1. Highest tier wins.
2. Ties broken by blockage-ladder rung order
   (`control_plane → host_process → broker → account_safety →
   account_owner → reconciliation → preflight → trading_session →
   runtime_freshness`) — the ladder already encodes "what blocks
   everything else."

The losing criticals are not hidden: the dominant banner carries a
"+N more critical" affordance that opens the ladder with every
concurrent critical listed. One click away is the accepted cost;
simultaneous banners are not.

### 3. "Information section" is the quiet status region — not PRD #951's documentation section

PRD #951's lower documentation section is contractually **"no CTAs, no
live claims."** A live `info` notice is a live claim and therefore can
never render there. The `info` destination in the placement function is
the quiet status region (session card tier). This resolves the
2026-07-08 grilling's naming collision explicitly: "demote to the
information section" means the quiet status region.

### 4. Arbitration is backend-authored

Consistent with ADR-0013's verbatim rule: the winner of the banner slot
is computed server-side on the same projection that authors the notices
(the frontend must not re-derive dominance from the notice list). The
"+N more" count and the folded list are part of the same projection.

## Consequences

**Positive:**
- The operator reads one headline and trusts that it is the most
  important fact on the page. Alarm fatigue stops being trained.
- The original field complaint is structurally fixed: non-actionable
  noise (`info`, quiet warnings) can no longer occupy alarm surfaces,
  and every alarm that does render carries its actionability and
  resolution honestly (ADR-0015 amendment).
- The taxonomy memo's open placement/severity question is closed.

**Negative (accepted):**
- A second concurrent critical is one click away instead of immediately
  visible. Accepted deliberately: stacked banners communicate panic,
  not priority, and the "+N more critical" affordance keeps the fold
  honest.
- Migration cost: the four existing banner surfaces must be rewired to
  consume one arbitrated projection instead of rendering independently.

**Non-consequences:**
- ADR-0015's notice schema and exhaustiveness gate are unchanged beyond
  its 2026-07-08 amendment.
- The blockage ladder, attention dropdown, trader guidance, and
  incidents panel keep their roles; this ADR only governs which tier ×
  actionability combinations may appear on which surface.
- PRD #951's lower documentation section is unchanged (and explicitly
  protected from live notices by §3).

## References

- `docs/architecture/adrs/0015-operator-notice-contract.md` § Amendment
  2026-07-08 — the actionability and resolution axes.
- `PythonDataService/app/operator/notices/schema.py` — the notice
  schema the placement function consumes.
- `PythonDataService/app/services/operator_blockage_ladder.py` — the
  rung order used as the arbitration tiebreak.
- `CONTEXT.md` § "Operator notice actionability & resolution (resolved
  2026-07-08)" and § "Single dominant headline (resolved 2026-07-08)".
