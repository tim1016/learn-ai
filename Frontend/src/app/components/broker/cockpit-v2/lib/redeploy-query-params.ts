import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

export function redeployQueryParamsForStatus(
  status: LiveInstanceStatus,
): Record<string, string> {
  const params: Record<string, string> = {};
  if (status.provenance) {
    if (status.provenance.strategy_spec_path) {
      params['spec'] = status.provenance.strategy_spec_path;
    }
    if (status.provenance.qc_audit_copy_path) {
      params['audit'] = status.provenance.qc_audit_copy_path;
    }
    if (status.provenance.qc_cloud_backtest_id) {
      params['backtest_id'] = status.provenance.qc_cloud_backtest_id;
    }
    if (status.provenance.account_id) params['account'] = status.provenance.account_id;
    params['parent_run_id'] = status.provenance.run_id;
    params['strategy_instance_id'] = status.strategy_instance_id;
  }
  if (status.start_defaults?.strategy) params['strategy'] = status.start_defaults.strategy;
  return params;
}
