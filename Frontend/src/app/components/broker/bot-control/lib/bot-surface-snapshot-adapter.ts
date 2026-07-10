import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

export function isLiveInstanceStatus(
  value: unknown,
  expectedInstanceId?: string,
): value is LiveInstanceStatus {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  const surface = record['operator_surface'];
  return (
    typeof record['strategy_instance_id'] === 'string' &&
    (expectedInstanceId === undefined || record['strategy_instance_id'] === expectedInstanceId) &&
    typeof record['stream_epoch'] === 'string' &&
    typeof record['surface_version'] === 'number' &&
    Number.isSafeInteger(record['surface_version']) &&
    record['surface_version'] >= 0 &&
    typeof record['fetched_at_ms'] === 'number' &&
    Number.isSafeInteger(record['fetched_at_ms']) &&
    record['fetched_at_ms'] >= 0 &&
    'latest_mutation' in record &&
    (record['latest_mutation'] === null ||
      (typeof record['latest_mutation'] === 'object' && record['latest_mutation'] !== null)) &&
    typeof record['process'] === 'object' &&
    record['process'] !== null &&
    typeof surface === 'object' &&
    surface !== null &&
    'host_process' in surface &&
    typeof surface.host_process === 'object' &&
    surface.host_process !== null &&
    'trader_guidance' in surface &&
    typeof surface.trader_guidance === 'object' &&
    surface.trader_guidance !== null &&
    typeof record['daily_lifecycle'] === 'object' &&
    record['daily_lifecycle'] !== null &&
    typeof record['lifecycle_chart'] === 'object' &&
    record['lifecycle_chart'] !== null
  );
}
