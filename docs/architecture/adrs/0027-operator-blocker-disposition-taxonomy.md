# ADR-0027: Operator blocker disposition taxonomy

**Status**: Accepted 2026-07-09.
**Related**: ADR-0013 (operator-surface judgment vs evidence), ADR-0015
(operator notice contract), ADR-0025 (single dominant headline),
ADR-0026 (daily bot lifecycle), deploy-preflight operator-blocker PRD
2026-07-09.

## Context

Deploy preflight and bot control used to answer the same operator
question differently: "what blocks this bot, and what is my move?"
Deploy could show broker or daemon facts without hard-blocking the
start path, while bot control could show raw gates, disabled actions,
or terminal states without a single honest cure. That drift let
conditions such as broker disconnect, fleet contamination, orphaned
sockets, and retired/poisoned bots render as unrelated UI problems.

ADR-0013 already requires the backend to author operator judgments.
ADR-0025 already requires a single dominant headline. This ADR names
the missing atom both surfaces consume.

## Decision

Introduce `OperatorBlocker` as the shared backend-authored blocker atom
for deploy preflight and bot control. Every blocker carries exactly one
closed disposition:

| Disposition | Meaning | Move rule |
|---|---|---|
| `fix_here` | The current surface can perform the cure. | Must carry `primary_move`. |
| `fix_elsewhere` | A real cure exists, but it lives off this surface. | Must carry `primary_move`. |
| `wait` | The block is transient or self-healing. | Must not carry `primary_move`. |
| `terminal` | This bot cannot be recovered in place. | Must carry at least one terminal move. |

The schema enforces the move rule. Frontend code renders blocker
headline, detail, labels, and move targets verbatim; it does not derive
copy or a cure from reason codes.

`blockers[0]` owns the single visible verb on the bot control verdict
card unless it is terminal. A terminal blocker owns the card completely:
no lifecycle verb, no hopeful remediation, only terminal moves such as
Replace or Remove. Deploy preflight refuses to proceed whenever any
blocking blocker is present.

## Consequences

- Broker disconnect has one contract on both surfaces:
  `broker_disconnected`, `fix_elsewhere`, `Connect the broker`.
- Fleet contamination routes to Account Monitor because fleet state is
  account-scoped, not a bot-local fix.
- `registry_amnesia` and `orphaned_socket` route to launcher/session
  runbooks because the honest cure is outside the bot card.
- `wait` conditions are allowed to block without a fake button.
- Retired and poisoned bots are not presented as recoverable. The UI
  can offer Replace and Remove, but not Resume, Start, or Reconcile.

Adding a new blocker requires adding the backend authoring case, a test
for the disposition/move pairing, and any surface routing needed for
the declared `OperatorAction`.
