import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import type {
  StrategyValidationCatalog,
  StrategyValidationDetail,
  StrategyValidationFlagEvent,
} from '../../services/strategy-validation.types';
import { StrategyValidationService } from '../../services/strategy-validation.service';
import { StrategyValidationComponent } from './strategy-validation.component';

const ACCEPTED_FLAG_EVENT: StrategyValidationFlagEvent = {
  event_id: 'seed-deployment-validation-accepted-for-deploy',
  strategy_key: 'deployment_validation',
  flag: 'validated',
  flagged_by: 'migration:strategy-validation-prd-seed',
  flagged_at_ms: 1775088000000,
  reason: 'Accepted for deployment.',
  behavioral_equivalence: {
    verdict: 'accepted_for_deploy',
    detail: 'Human validation accepted the current engine evidence for deployment.',
  },
  evidence_snapshot: {
    settings_file_ref: 'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json',
    settings_file_sha256: 'spec-sha',
    qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
    audit_copy_ref: 'references/qc-shadow/DeploymentValidationAlgorithm.py',
    audit_copy_sha256: 'audit-sha',
    reconciliation_ref: 'references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md',
    validation_case_symbol: 'SPY',
    reconciliation_status: 'passed',
    diagnostics: null,
  },
  evidence_snapshot_sha256: 'snapshot-sha',
  superseded_by_event_id: null,
};

const DEPLOYMENT_DETAIL: StrategyValidationDetail = {
  strategy_key: 'deployment_validation',
  display_name: 'Deployment Validation',
  description: 'Two-green-minute deployment validation primitive.',
  validation_state: 'validated',
  deployable: true,
  settings_file_ref: 'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json',
  settings_file_sha256: 'spec-sha',
  qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
  audit_copy_ref: 'references/qc-shadow/DeploymentValidationAlgorithm.py',
  audit_copy_sha256: 'audit-sha',
  reconciliation_ref: 'references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md',
  validation_case_symbol: 'SPY',
  reconciliation_status: 'passed',
  diagnostics: {
    verdict: 'passed',
    trades_matched: 56,
    trades_validated: 56,
    pnl_max_abs_diff: '0.00',
    divergence_counts: { fill_price_drift: 2 },
    notes: ['QC receipt stored.'],
  },
  behavioral_equivalence: ACCEPTED_FLAG_EVENT.behavioral_equivalence,
  current_flag_event: ACCEPTED_FLAG_EVENT,
  flag_events: [ACCEPTED_FLAG_EVENT],
  reference_code: {
    path: 'references/qc-shadow/DeploymentValidationAlgorithm.py',
    sha256: 'audit-sha',
    language: 'python',
    source: 'class DeploymentValidationAlgorithm(QCAlgorithm):\n    pass\n',
  },
};

const ORB_DETAIL: StrategyValidationDetail = {
  strategy_key: 'spy_orb',
  display_name: 'Opening Range Breakout',
  description: 'Opening range breakout strategy.',
  validation_state: 'needs_validation',
  deployable: false,
  settings_file_ref: null,
  settings_file_sha256: null,
  qc_cloud_backtest_id: null,
  audit_copy_ref: null,
  audit_copy_sha256: null,
  reconciliation_ref: null,
  validation_case_symbol: null,
  reconciliation_status: null,
  diagnostics: null,
  behavioral_equivalence: null,
  current_flag_event: null,
  flag_events: [],
  reference_code: null,
};

const CATALOG: StrategyValidationCatalog = {
  strategies: [
    DEPLOYMENT_DETAIL,
    ORB_DETAIL,
  ],
};

class FakeStrategyValidationService {
  getCatalog = vi.fn().mockResolvedValue(CATALOG);
  getDetail = vi.fn((key: string) => Promise.resolve(key === 'deployment_validation' ? DEPLOYMENT_DETAIL : ORB_DETAIL));
  refreshValidationEvidence = vi.fn((key: string) =>
    Promise.resolve({
      refresh_id: `manifest-evidence:${key}:123`,
      refreshed_at_ms: 123,
      detail: key === 'deployment_validation' ? DEPLOYMENT_DETAIL : ORB_DETAIL,
    }),
  );
  flagValidation = vi.fn((key: string) =>
    Promise.resolve({
      ...(key === 'deployment_validation' ? DEPLOYMENT_DETAIL : ORB_DETAIL),
      validation_state: 'needs_validation',
      deployable: false,
      current_flag_event: {
        ...ACCEPTED_FLAG_EVENT,
        flag: 'invalidated',
        reason: 'Reject this evidence.',
        behavioral_equivalence: {
          verdict: 'rejected',
          detail: 'Human validation rejected this strategy for deployment.',
        },
      },
    }),
  );
}

describe('StrategyValidationComponent', () => {
  it('renders validated and unvalidated strategies in the catalog', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    expect(await screen.findByRole('heading', { name: 'Strategy Validation' })).toBeTruthy();
    expect(await screen.findByRole('button', { name: /Deployment Validation/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Opening Range Breakout/ })).toBeTruthy();
    expect(screen.getAllByText('Validated').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Needs validation').length).toBeGreaterThan(0);
  });

  it('opens detail with QC evidence and reference code without rendering internal port source', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    expect(await screen.findByText('d2fe45a7142e88575f6fbd75229f8681')).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('56 trades matched')).toBeTruthy();
    expect(screen.getByText('56 trades validated')).toBeTruthy();
    expect(screen.getByText('Fill Price Drift')).toBeTruthy();
    expect(screen.queryByText('fill_price_drift')).toBeNull();
    expect(screen.getAllByText('Accepted For Deploy').length).toBeGreaterThan(0);
    expect(screen.getByText('migration:strategy-validation-prd-seed')).toBeTruthy();
    expect(screen.getByText('snapshot-sha')).toBeTruthy();
    expect(screen.getByText(/class DeploymentValidationAlgorithm/)).toBeTruthy();
    expect(screen.queryByText(/DeploymentValidationConsecutiveGreen/)).toBeNull();
  });

  it('links the selected strategy directly into Engine Lab validation mode', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    const link = await screen.findByRole('link', { name: /Validate in Engine Lab/ });

    expect(link.getAttribute('href')).toContain('/engine?');
    expect(link.getAttribute('href')).toContain('strategy=deployment_validation');
    expect(link.getAttribute('href')).toContain('engine=both');
    expect(link.getAttribute('href')).toContain('symbol=SPY');
  });

  it('switches to the selected strategy detail', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Opening Range Breakout/ }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Opening Range Breakout' })).toBeTruthy();
    });
    expect(screen.getAllByText('Needs validation').length).toBeGreaterThan(0);
    expect(screen.getByText('Validation evidence has not been registered yet.')).toBeTruthy();
  });

  it('refreshes validation evidence for the selected strategy', async () => {
    const service = new FakeStrategyValidationService();
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Refresh evidence' }));

    await waitFor(() => {
      expect(service.refreshValidationEvidence).toHaveBeenCalledWith('deployment_validation');
    });
    expect(await screen.findByText(/Validation evidence refreshed/)).toBeTruthy();
  });

  it('requires a reason and then saves the selected validation flag', async () => {
    const service = new FakeStrategyValidationService();
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Save flag' }));
    expect((await screen.findByRole('alert')).textContent).toContain('A validation reason is required.');

    fireEvent.click(screen.getByLabelText('Reject'));
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'Reject this evidence.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));

    await waitFor(() => {
      expect(service.flagValidation).toHaveBeenCalledWith('deployment_validation', {
        flag: 'invalidated',
        reason: 'Reject this evidence.',
      });
    });
  });

  it('attaches a backtest ID when accepting reconciled evidence', async () => {
    const service = new FakeStrategyValidationService();
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Opening Range Breakout/ }));
    await screen.findByRole('heading', { name: 'Opening Range Breakout' });
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'Trades match within the accepted gate.' } });
    fireEvent.input(screen.getByLabelText('QC Cloud backtest ID'), { target: { value: 'qc-backtest-42' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));

    await waitFor(() => {
      expect(service.flagValidation).toHaveBeenCalledWith('spy_orb', {
        flag: 'validated',
        reason: 'Trades match within the accepted gate.',
        qc_cloud_backtest_id: 'qc-backtest-42',
      });
    });
  });
});
