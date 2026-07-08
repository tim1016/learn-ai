import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

export function boundRunIdForStatus(status: LiveInstanceStatus): string | null {
  return status.live_binding?.run_id ?? status.evidence_binding?.run_id ?? null;
}
