import type { HostProcessStartCapability } from '../../../../api/live-instances.types';
import type { HostRunnerActionResponse } from '../../../../api/live-runs.types';
import { LiveRunsService } from '../../../../services/live-runs.service';

export type StartableHostProcessCapability = HostProcessStartCapability & {
  enabled: true;
  run_id: string;
  request: NonNullable<HostProcessStartCapability['request']>;
};

export function canStartHostProcess(
  capability: HostProcessStartCapability,
): capability is StartableHostProcessCapability {
  return capability.enabled && capability.run_id !== null && capability.request !== null;
}

export async function startHostProcessFromCapability(
  liveRuns: LiveRunsService,
  capability: StartableHostProcessCapability,
): Promise<HostRunnerActionResponse> {
  return await liveRuns.startHostRunner(capability.run_id, capability.request);
}
