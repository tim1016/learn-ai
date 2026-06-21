// PRD #617 — classify-readiness-transition pure function.
//
// Frontend-only derivation over polling-delta state.  The server cannot
// author this classification because it lacks the previous-poll
// snapshot — see ADR-0013 §4 "Frontend-allowed derivations".
//
// The closed result vocabulary drives the auto-tab-selection rule
// (PRD #617 §"User Stories" 7, 10): force Status & Risk exactly once
// on `entered-attention`, never on `attention-changed` or `stable`.

import type { ReadinessVerdictEnum } from '../../../../api/live-instances.types';

export type ReadinessTransition =
  | 'initial'
  | 'entered-attention'
  | 'attention-changed'
  | 'recovered'
  | 'stable';

const ATTENTION_VERDICTS: ReadonlySet<ReadinessVerdictEnum> = new Set([
  'BLOCKED',
  'DEGRADED',
  'UNKNOWN',
]);

/**
 * Classify the transition between two readiness verdicts.
 *
 * - `initial`            — first observation; previous is null.
 * - `entered-attention`  — previous READY → current non-READY.
 * - `attention-changed`  — previous non-READY → current non-READY,
 *                          different verdict (e.g. BLOCKED → DEGRADED).
 * - `recovered`          — previous non-READY → current READY.
 * - `stable`             — no meaningful change.
 *
 * READY is the only "calm" state; everything else is attention.
 */
export function classifyReadinessTransition(
  previous: ReadinessVerdictEnum | null,
  current: ReadinessVerdictEnum,
): ReadinessTransition {
  if (previous === null) {
    return 'initial';
  }
  const prevIsAttention = ATTENTION_VERDICTS.has(previous);
  const currIsAttention = ATTENTION_VERDICTS.has(current);
  if (!prevIsAttention && currIsAttention) {
    return 'entered-attention';
  }
  if (prevIsAttention && !currIsAttention) {
    return 'recovered';
  }
  if (prevIsAttention && currIsAttention && previous !== current) {
    return 'attention-changed';
  }
  return 'stable';
}

/**
 * Pin every readiness verdict to a known case so the closed enum
 * grows with the wire contract.  The static type forces an unhandled
 * verdict to fail to compile.
 */
export function _assertExhaustive(_: never): never {
  throw new Error(`Unhandled readiness verdict: ${_ as string}`);
}
