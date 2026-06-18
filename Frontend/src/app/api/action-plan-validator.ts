/**
 * Client-side validation that mirrors the Pydantic ``ActionPlan`` model
 * in ``PythonDataService/app/schemas/action_plan.py``. Used by the deploy
 * form picker to surface inline errors before the operator hits submit;
 * the server-side Pydantic is the authoritative gate.
 *
 * Cross-language parity is asserted via the shared JSON fixtures under
 * ``PythonDataService/tests/fixtures/action_plan/`` — Python via
 * Pydantic, frontend via Vitest + ``validateActionPlan``. A fixture that
 * passes here must pass Pydantic; a fixture that fails here must fail
 * Pydantic with a matching error category.
 *
 * Slice 1B (#595) — stock legs + ``close_leg``. Selectors and option
 * legs land in Slice 1C (#596) and extend ``ActionPlanIssueCode``.
 */

import type { ActionPlan } from './action-plan.types';

export type ActionPlanIssueCode =
  | 'missing_leg_id'
  | 'malformed_leg_id'
  | 'duplicate_leg_id'
  | 'missing_underlying'
  | 'invalid_qty_ratio'
  | 'orphan_close_leg'
  | 'unknown_kind';

export interface ActionPlanIssue {
  code: ActionPlanIssueCode;
  /** Operator-readable explanation suitable for picker error rows. */
  message: string;
  /** Originating ``leg_id`` when locatable; null for plan-level
   * issues (e.g. duplicate detection runs across legs). */
  legId: string | null;
}

const LEG_ID_RE = /^[a-z0-9_]{1,32}$/;

/** Validates a candidate plan against the Slice-1B schema. Returns the
 * full issue list (not just the first error) so the picker can render
 * all problems at once. */
export function validateActionPlan(plan: unknown): ActionPlanIssue[] {
  const issues: ActionPlanIssue[] = [];
  if (!isPlanShape(plan)) {
    issues.push({ code: 'unknown_kind', message: 'action plan must have on_enter and on_exit lists', legId: null });
    return issues;
  }

  const legIds = new Set<string>();
  for (const leg of plan.on_enter) {
    const legId = typeof leg.leg_id === 'string' ? leg.leg_id : null;
    if (!legId) {
      issues.push({ code: 'missing_leg_id', message: 'every entry leg requires a leg_id', legId: null });
    } else if (!LEG_ID_RE.test(legId)) {
      issues.push({
        code: 'malformed_leg_id',
        message: `leg_id "${legId}" must match ${LEG_ID_RE.source}`,
        legId,
      });
    } else if (legIds.has(legId)) {
      issues.push({
        code: 'duplicate_leg_id',
        message: `leg_id "${legId}" is declared more than once`,
        legId,
      });
    } else {
      legIds.add(legId);
    }

    const underlying = leg.instrument?.underlying;
    if (typeof underlying !== 'string' || underlying.length === 0) {
      issues.push({
        code: 'missing_underlying',
        message: 'every leg requires an explicit instrument.underlying (no fallback from live_config.symbol)',
        legId,
      });
    }

    if (typeof leg.qty_ratio !== 'number' || !Number.isInteger(leg.qty_ratio) || leg.qty_ratio < 1) {
      issues.push({
        code: 'invalid_qty_ratio',
        message: 'qty_ratio must be a positive integer (>= 1)',
        legId,
      });
    }
  }

  for (const exit of plan.on_exit) {
    if (exit.kind === 'close_leg' && !legIds.has(exit.entry_leg_id)) {
      issues.push({
        code: 'orphan_close_leg',
        message: `close_leg references unknown entry_leg_id "${exit.entry_leg_id}"`,
        legId: exit.entry_leg_id,
      });
    }
  }
  return issues;
}

function isPlanShape(plan: unknown): plan is ActionPlan {
  if (typeof plan !== 'object' || plan === null) return false;
  const p = plan as Record<string, unknown>;
  return Array.isArray(p['on_enter']) && Array.isArray(p['on_exit']);
}
