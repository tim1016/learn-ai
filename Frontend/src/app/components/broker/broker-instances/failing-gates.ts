import type { ReadinessVector } from '../../../api/live-instances.types';

/**
 * Operator-facing projection of a failing readiness gate. Both
 * `<app-can-it-trade-card>` and `<app-pre-trade-checklist>` render this
 * shape, so the projection lives in one place — same labels, same filter,
 * same row shape across both surfaces.
 */
export interface FailingGateRow {
  key: string;
  label: string;
  severity: 'hard' | 'soft';
  detail: string;
}

/**
 * Project a readiness vector into the failing-gate rows the cockpit
 * surfaces care about. Returns `[]` when there is no vector. Maps each
 * gate's technical `name` through the operator-language `labels` map,
 * falling back to the raw name on miss so an engine-emitted gate the
 * frontend has not labelled yet still renders (badly, but visibly).
 */
export function projectFailingGates(
  readiness: ReadinessVector | null,
  labels: Record<string, string>,
): FailingGateRow[] {
  if (!readiness) return [];
  return readiness.gates
    .filter((g) => g.status !== 'pass')
    .map<FailingGateRow>((g) => ({
      key: g.name,
      label: labels[g.name] ?? g.name,
      severity: g.severity,
      detail: g.detail,
    }));
}
