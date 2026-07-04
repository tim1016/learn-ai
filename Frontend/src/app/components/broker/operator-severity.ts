import type { OperatorSurfaceConditionSeverity } from '../../api/live-instances.types';

export type OperatorDisplaySeverity = OperatorSurfaceConditionSeverity;
export type PrimeTagSeverity = 'success' | 'info' | 'warn' | 'danger' | 'secondary';
export type OperatorPillTone = 'ok' | 'attention' | 'muted';

export function operatorTagSeverity(
  severity: OperatorDisplaySeverity,
): PrimeTagSeverity {
  switch (severity) {
    case 'ok':
      return 'success';
    case 'info':
      return 'info';
    case 'warning':
      return 'warn';
    case 'critical':
      return 'danger';
    case 'neutral':
      return 'secondary';
  }
}

export function operatorPillTone(
  severity: OperatorDisplaySeverity,
): OperatorPillTone {
  switch (severity) {
    case 'ok':
      return 'ok';
    case 'warning':
    case 'critical':
      return 'attention';
    case 'info':
    case 'neutral':
      return 'muted';
  }
}
