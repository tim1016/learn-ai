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
      },
      start_defaults: {
        strategy: 'deployment_validation',
      },
    } as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({
      spec: 'specs/dia.json',
      audit: 'audits/dia.json',
      backtest_id: 'bt-123',
      account: 'DU123',
      parent_run_id: 'parent-run',
      strategy_instance_id: 'DEPVAL-DIA-20260626',
      strategy: 'deployment_validation',
    });
  });

  it('omits absent optional provenance fields', () => {
    const status = {
      strategy_instance_id: 'BOT',
      provenance: null,
      start_defaults: null,
    } as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({});
  });
});
