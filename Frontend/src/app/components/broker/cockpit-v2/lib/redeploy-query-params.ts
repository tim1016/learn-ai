import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

export function redeployQueryParamsForStatus(
  status: LiveInstanceStatus,
): Record<string, string> {
  const params: Record<string, string> = {};
  if (status.provenance) {
    if (status.provenance.strategy_spec_path) {
      params['spec_path'] = status.provenance.strategy_spec_path;
    }
    if (status.provenance.qc_audit_copy_path) {
      params['qc_audit_copy_path'] = status.provenance.qc_audit_copy_path;
    }
    if (status.provenance.qc_cloud_backtest_id) {
      params['qc_backtest_id'] = status.provenance.qc_cloud_backtest_id;
    }
    const signalStream = status.provenance.live_config?.['symbol'];
    if (typeof signalStream === 'string' && signalStream.trim() !== '') {
      params['signal_stream'] = signalStream.trim().toUpperCase();
    }
    params['parent_run_id'] = status.provenance.run_id;
    params['instance_id'] = status.strategy_instance_id;
  }
  if (status.start_defaults?.strategy) {
    params['strategy_key'] = status.start_defaults.strategy;
  }
  return params;
}
