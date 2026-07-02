import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { redeployQueryParamsForStatus } from './redeploy-query-params';

describe('redeployQueryParamsForStatus', () => {
  it('projects provenance and start defaults into deploy query params', () => {
    const status = {
      strategy_instance_id: 'DEPVAL-DIA-20260626',
      provenance: {
        strategy_spec_path: 'specs/dia.json',
        qc_audit_copy_path: 'audits/dia.json',
        qc_cloud_backtest_id: 'bt-123',
        account_id: 'DU123',
        run_id: 'parent-run',
        live_config: { symbol: 'dia' },
      },
      start_defaults: {
        strategy: 'deployment_validation',
      },
    } as unknown as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({
      spec_path: 'specs/dia.json',
      qc_audit_copy_path: 'audits/dia.json',
      qc_backtest_id: 'bt-123',
      signal_stream: 'DIA',
      parent_run_id: 'parent-run',
      instance_id: 'DEPVAL-DIA-20260626',
      strategy_key: 'deployment_validation',
    });
  });

  it('omits absent optional provenance fields', () => {
    const status = {
      strategy_instance_id: 'BOT',
      provenance: null,
      start_defaults: null,
    } as unknown as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({});
  });
});
