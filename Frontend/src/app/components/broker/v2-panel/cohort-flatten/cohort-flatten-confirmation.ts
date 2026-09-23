/**
 * The closed operator-copy map for cohort flatten (#1909).
 *
 * Owner decision 2026-09-23: this surface's fixed wording stays in the
 * frontend, but only here — one `as const` map with a closed set of keys, per
 * the CLAUDE.md rule that operator copy arrives "from the backend or from a
 * closed operator-copy map". The cohort-flatten components render copy only
 * from this map (pinned by `cohort-flatten-confirmation.spec.ts`); the
 * functions in it interpolate backend facts only — account, counts, per-leg
 * attributed exposure, the frozen wave's strategy label and symbol — never
 * operator prose built elsewhere. Backend-authored refusal prose
 * (`message`/`why`) is rendered as-is and never passes through this map.
 *
 * The consequence's claim that no other bot's or manual position is touched
 * must stay true to ADR 0051's per-leg semantics (Decision 1): every leg runs
 * the unchanged per-bot pipeline for its own `strategy_instance_id`
 * (`execute_cohort_legs` → `panel_data_source.run_action` in
 * `PythonDataService/app/services/broker_v2_panel/cohort_execution.py`), whose
 * flatten performers reduce only that bot's Clerk-attributed exposure (PRD
 * #1752 US2; the account-level broker flatten that would net manual positions
 * is the alternative ADR 0051 rejects). If that ever changes, this copy must.
 */

import { fmtExposure } from '../../format';
import type { CohortFlattenLeg, CohortLegResult } from '../lib/broker-v2-panel.types';

/**
 * The most legs one cohort-flatten POST may carry.
 *
 * Mirrors `CohortFlattenRequest.legs` — `Field(min_length=1, max_length=64)`
 * in `PythonDataService/app/schemas/broker_v2_panel.py`, published as
 * `maxItems: 64` in the OpenAPI contract. The generated TS types do not carry
 * array bounds, so the limit is named here once rather than inlined.
 */
export const MAX_COHORT_FLATTEN_LEGS = 64;

function bots(count: number): string {
  return count === 1 ? 'bot' : 'bots';
}


export const COHORT_FLATTEN_COPY = {
  // ── Drawer ──────────────────────────────────────────────────────────────
  eyebrow: (accountId: string) => `Roster · ${accountId}`,
  title: 'Flatten a cohort',
  intro:
    'Bots on the same strategy and symbol, each flattened by its own attributed ' +
    'action. A bot can join the batch only when its own flatten is available.',
  closeLabel: 'Close flatten a cohort',
  reading: 'Reading the roster…',
  loadFailed: 'Could not read the account’s cohorts. Close and reopen to retry.',
  empty: 'No cohort on this account has two or more bots on the same strategy and symbol.',

  // ── Cohort group ────────────────────────────────────────────────────────
  selectCohort: 'Select this cohort',
  selectCohortLabel: (strategyLabel: string, symbol: string) =>
    `Select this cohort: ${strategyLabel} ${symbol}`,
  armedCount: (armed: number, total: number) => `${armed} of ${total} can flatten`,
  // The roster's single exposure formatter, so a leg reads the same here as
  // on every other surface that shows attributed exposure.
  exposure: fmtExposure,
  capReached: (max: number) =>
    `A batch can carry at most ${max} bots; the rest of this cohort stays unselected.`,

  // ── Review and confirmation (modelled on the per-bot flatten_stop copy) ──
  review: (count: number) => `Review flatten of ${count}`,
  inFlight: (count: number) => `Flattening ${count} ${bots(count)}…`,
  /** The per-bot `flatten_stop` confirmation's `required_token`, so one word
   * means one thing on both surfaces. */
  confirmToken: 'FLATTEN',
  confirmHeading: (count: number) => `Flatten ${count} ${bots(count)} in this cohort?`,
  confirmMessage: (accountId: string, strategyLabel: string, legs: readonly CohortFlattenLeg[]) =>
    `This command targets ${legs.length} ${bots(legs.length)} of ${strategyLabel} on account ` +
    `${accountId}. Attributed exposure: ` +
    legs.map((leg) => `${leg.strategy_instance_id} ${fmtExposure(leg.exposure)}`).join('; ') +
    '.',
  confirmConsequence:
    'Each bot runs its own flatten in turn and reduces only its Clerk-attributed ' +
    'exposure; no other bot’s or manual position on this account is touched. A ' +
    'refused bot does not stop the others, and fills may complete later.',
  confirmLabel: (count: number) => `Flatten ${count}`,

  // ── Dispatch ────────────────────────────────────────────────────────────
  retry: 'Retry this batch',
  requestUnknown:
    'The flatten request did not reach a result. Nothing is confirmed either way — ' +
    'retrying re-sends this same batch, so a bot that already flattened replays as a no-op.',
  requestRefused: 'The flatten batch was refused before any bot ran.',
  requestRefusedNext: 'Let the roster re-read, then start a new wave.',
  requestFallback: 'The flatten request was refused.',

  // ── Outcome ─────────────────────────────────────────────────────────────
  outcomeRegion: 'Flatten outcome',
  outcomeSummary: (
    counts: { applied: number; replayed: number; refused: number; failed: number },
    sent: number,
  ) =>
    `Applied ${counts.applied}, replayed ${counts.replayed}, refused ${counts.refused}, ` +
    `failed ${counts.failed} of ${sent} sent.`,
  outcomeLabel: {
    applied: 'Applied',
    replayed: 'Replayed (already applied)',
    refused: 'Refused',
    failed: 'Failed',
    unknown: 'Outcome unknown',
  } satisfies Readonly<Record<CohortLegResult['outcome'], string>>,
  receipt: 'Receipt',
  accountBlockerHeading: 'Batch stopped: account-scoped',
  accountBlockerBody: (attempted: number, sent: number) =>
    `The batch stopped after ${attempted} of ${sent} bots: this account refused further ` +
    'writes, so no later bot could run.',
  notAttempted: 'Not attempted:',
  staleRefusals: (count: number) =>
    `${count} ${count === 1 ? 'bot was' : 'bots were'} refused because what you confirmed ` +
    'changed after it was read. Retrying would re-send the same facts; let the roster ' +
    're-read and start a new wave.',
} as const;

export type CohortFlattenCopy = typeof COHORT_FLATTEN_COPY;
