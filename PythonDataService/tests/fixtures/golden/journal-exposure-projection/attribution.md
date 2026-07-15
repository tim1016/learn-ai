# Account Clerk journal exposure projection — golden fixture

## What it proves

`journal.json` is a tiny synthetic Account Clerk journal set covering a BUY,
SELL, redelivered execution, two namespaces, two account journals, a non-fill
callback, and a namespace that returns to zero. Its expected rows are the
independent hand-computed sum of signed fill quantities after deduplicating on
the broker's `exec_id`:

| Account | Namespace / instance | Symbol | Calculation | Expected |
| --- | --- | --- | --- | --- |
| DUA | bot-alpha | SPY | `+5 - 2` (redelivered `exec-a-alpha-sell` ignored) | `+3` |
| DUA | bot-beta | QQQ | `+2 - 2` | omitted (zero) |
| DUB | bot-alpha | SPY | `+2` (`exec-a-alpha-buy` is intentionally reused from DUA) | `+2` |

## Provenance

- **Input:** synthetic, deterministic Clerk journal rows.
- **Methodology:** the locked canonical-fill-fold decision in
  [issue #1038](https://github.com/tim1016/learn-ai/issues/1038), implemented
  by issue #1039.
- **Independent oracle:** the table above, hand-computed without calling
  `project_journal_exposure` (`reference_kind: hand_computed`).

## Regeneration

This is a discrete state-machine fixture, not a vendor or floating-point
reference capture. Update `journal.json` only when the locked journal-exposure
contract changes; recompute the table directly from the JSON rows and state
the contract change in the commit message.
