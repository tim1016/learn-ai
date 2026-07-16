# Account Observation Lease paper qualification — 2026-07-16

This is an interim qualification receipt, not cutover authorization.

## Scope

- Broker: IBKR paper
- Account: `DUM284968`
- Canonical NYSE session date: `2026-07-16`
- Runtime posture before destructive validation: connected, flat, Account
  Service in standby, no bot process

## Durable parity replay

The canonical replay command was:

```text
python -m scripts.archive_observation_lease_parity \
  --artifacts-root artifacts \
  --account-id DUM284968 \
  --output /tmp/observation-lease-parity-DUM284968-after-restart.json
```

Snapshot result:

- archive schema: `2`
- comparison schema: `2`
- generation authority: `account_clerk`
- qualifying comparisons: `4`
- qualifying session dates: `2026-07-16`
- invalid comparisons: `0`
- lease-weaker comparisons: `0`
- lease-stricter comparisons: `0`
- excluded legacy comparisons: `69`
- source event count: `1908`
- source SHA-256:
  `77f4f46adc8580588b76510c93456415ecd7515c8afa6837da2f1cbf1b7e4900`
- `cutover_ready`: `false`

The four qualifying comparisons are real submit-boundary rows from run
`125c98384e568e11e2de797b7c5b16bf0a9f416fa3cd89ac06d1dc1dcb933f0c`
for temporary validation bot `lease-val-20260716`. Each result was Account
Truth `pass` and observation lease `pass`.

## Account Service restart smoke

The host-native Account Service was terminated only while the paper account
was flat and no bot was running.

1. Generation 15 entered `DRAINING`; the Account Desk reported `FENCED`.
2. The daemon replaced it with generation 16 and returned to `ATTACHED` /
   `STANDBY` in about two seconds.
3. Generation 16 was terminated for a canonical-assessor sample.
4. Generation 17 became active while the persisted observation lease still
   named generation 16. `assess_account_observation_lease(...)` returned
   `REVOKED` with `ACCOUNT_CLERK_GENERATION_CHANGED`; the old verified
   artifact did not authorize the new Clerk.
5. After the next clean observer sweep, the assessor returned `VERIFIED` with
   lease generation 17 at `1784219811710` ms UTC.

This passes the Clerk-restart fail-closed and self-heal smoke without starting
a bot or placing an order.

## Decision

Do not enable `IBKR_ACCOUNT_GATE_AUTHORITY=observation_lease`. The restart
smoke is complete, but the durable replay has only one of the required three
distinct NYSE session dates. Two further paper sessions must contribute valid
v2 submit-boundary comparisons with no lease-weaker outcome. The final ready
archive must snapshot the then-current journal and pass `--require-ready`.
