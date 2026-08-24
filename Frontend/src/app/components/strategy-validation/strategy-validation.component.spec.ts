import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type {
  StrategyProofDossier,
  StrategyProofStage,
  StrategyValidationCatalog,
  StrategyValidationDetail,
  StrategyValidationFlagEvent,
} from '../../services/strategy-validation.types';
import { StrategyValidationService } from '../../services/strategy-validation.service';
import { StrategyValidationComponent } from './strategy-validation.component';

function proofStage(
  stageId: string,
  title: string,
  state: StrategyProofStage['state'],
): StrategyProofStage {
  return {
    stage_id: stageId,
    title,
    state,
    authority: 'Test authority',
    summary: `${title} summary.`,
    next_step: state === 'complete' || state === 'not_applicable' ? null : `Complete ${title}.`,
    actions: [],
    evidence: [],
  };
}

const CURRENT_HARNESS_PROOF: StrategyProofDossier = {
  state: 'current',
  completed_stages: 3,
  total_stages: 3,
  blocking_stage_id: null,
  blocking_summary: null,
  stages: [
    proofStage('program_contract', 'Signal Program contract', 'complete'),
    proofStage('reference_run', 'QuantConnect reference run', 'not_applicable'),
    proofStage('current_proof', 'Current validation proof', 'complete'),
  ],
};

const MISSING_PROOF: StrategyProofDossier = {
  state: 'missing',
  completed_stages: 0,
  total_stages: 2,
  blocking_stage_id: 'program_contract',
  blocking_summary: 'Promote and qualify the Signal Program.',
  stages: [
    proofStage('program_contract', 'Signal Program contract', 'missing'),
    proofStage('current_proof', 'Current validation proof', 'missing'),
  ],
};

const EMA_PROOF: StrategyProofDossier = {
  state: 'missing',
  completed_stages: 1,
  total_stages: 2,
  blocking_stage_id: 'reference_run',
  blocking_summary: 'Run the QuantConnect reference backtest.',
  stages: [
    proofStage('reference_source', 'QuantConnect reference algorithm', 'complete'),
    {
      ...proofStage('reference_run', 'QuantConnect reference run', 'missing'),
      actions: [
        {
          kind: 'external_link',
          label: 'How to run a backtest and find its ID',
          href: 'https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/getting-started',
        },
      ],
    },
  ],
};

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
    validator_code_ref: 'PythonDataService/app/lean_sidecar/trusted_samples/deployment_validation.py',
    validator_code_sha256: 'validator-sha',
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
  strategy_category: 'operational_validation_harness',
  validation_state: 'validated',
  deployable: true,
  proof: CURRENT_HARNESS_PROOF,
  validator_code_ref: 'PythonDataService/app/lean_sidecar/trusted_samples/deployment_validation.py',
  validator_code_sha256: 'validator-sha',
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
    recorded_sha256: 'audit-sha',
    state: 'current',
    language: 'python',
    source: 'class DeploymentValidationAlgorithm(QCAlgorithm):\n    pass\n',
  },
};

const ORB_DETAIL: StrategyValidationDetail = {
  strategy_key: 'spy_orb',
  display_name: 'Opening Range Breakout',
  description: 'Opening range breakout strategy.',
  strategy_category: 'production_candidate',
  validation_state: 'needs_validation',
  deployable: false,
  proof: MISSING_PROOF,
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

const EMA_DETAIL: StrategyValidationDetail = {
  strategy_key: 'ema_crossover_signal',
  display_name: 'EMA Crossover Signal',
  description: 'Canonical SPY EMA crossover signal.',
  strategy_category: 'production_candidate',
  validation_state: 'needs_validation',
  deployable: false,
  proof: EMA_PROOF,
  settings_file_ref: null,
  settings_file_sha256: null,
  qc_cloud_backtest_id: null,
  audit_copy_ref: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
  audit_copy_sha256: 'audit-sha',
  reconciliation_ref: null,
  validation_case_symbol: 'SPY',
  reconciliation_status: null,
  diagnostics: null,
  behavioral_equivalence: null,
  current_flag_event: null,
  flag_events: [],
  reference_code: {
    path: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
    sha256: 'audit-sha',
    recorded_sha256: 'audit-sha',
    state: 'current',
    language: 'python',
    source: 'class SpyEmaCrossoverAlgorithm(QCAlgorithm):\n    pass\n',
  },
};

const CATALOG: StrategyValidationCatalog = {
  strategies: [
    DEPLOYMENT_DETAIL,
    EMA_DETAIL,
    ORB_DETAIL,
  ],
};

const DETAIL_BY_KEY: Record<string, StrategyValidationDetail> = {
  deployment_validation: DEPLOYMENT_DETAIL,
  ema_crossover_signal: EMA_DETAIL,
  spy_orb: ORB_DETAIL,
};

class FakeStrategyValidationService {
  getCatalog = vi.fn().mockResolvedValue(CATALOG);
  getDetail = vi.fn((key: string) => Promise.resolve(DETAIL_BY_KEY[key] ?? ORB_DETAIL));
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
    expect(screen.getAllByText('Proof current').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Proof missing').length).toBeGreaterThan(0);
  });

  it('renders the deployment test program as a non-Live operational harness', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    expect(await screen.findByRole('heading', { name: 'Operational validation harness' })).toBeTruthy();
    expect(screen.getByText(/permanently ineligible for Live/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Strategy proof' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Current validation proof' })).toBeTruthy();
    expect(screen.getAllByText('Not Applicable').length).toBeGreaterThan(0);
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('56 trades matched')).toBeTruthy();
    expect(screen.getByText('56 trades validated')).toBeTruthy();
    expect(screen.getByText('Fill Price Drift')).toBeTruthy();
    expect(screen.queryByText('fill_price_drift')).toBeNull();
    expect(screen.getAllByText('Accepted For Deploy').length).toBeGreaterThan(0);
    expect(screen.getByText('migration:strategy-validation-prd-seed')).toBeTruthy();
    expect(screen.getByText('snapshot-sha')).toBeTruthy();
    expect(screen.queryByText(/class DeploymentValidationAlgorithm/)).toBeNull();
    expect(screen.queryByText(/DeploymentValidationConsecutiveGreen/)).toBeNull();
  });

  it('links the selected strategy directly into Strategy Lab validation mode', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    const link = await screen.findByRole('link', { name: /Diagnose in Strategy Lab/ });

    expect(link.getAttribute('href')).toContain('/strategy-lab?');
    expect(link.getAttribute('href')).toContain('strategy=deployment_validation');
    expect(link.getAttribute('href')).toContain('engine=both');
    expect(link.getAttribute('href')).toContain('symbol=SPY');
  });

  it('links accepted evidence into the broker-aware Alpaca Deploy trader flow', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    const link = await screen.findByRole('link', { name: /Deploy strategy/ });

    expect(link.getAttribute('href'))
      .toBe('/brokers/alpaca?deploy=&strategy=deployment_validation');
  });

  it('does not treat deploy binding as a LEAN validator when validator evidence is missing', async () => {
    const legacyDetail: StrategyValidationDetail = {
      ...DEPLOYMENT_DETAIL,
      validator_code_ref: null,
      validator_code_sha256: null,
      current_flag_event: {
        ...ACCEPTED_FLAG_EVENT,
        evidence_snapshot: {
          ...ACCEPTED_FLAG_EVENT.evidence_snapshot,
          validator_code_ref: null,
          validator_code_sha256: null,
        },
      },
    };
    const service = new FakeStrategyValidationService();
    service.getCatalog.mockResolvedValue({ strategies: [legacyDetail, ORB_DETAIL] });
    service.getDetail.mockResolvedValue(legacyDetail);

    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    const link = await screen.findByRole('link', { name: /Diagnose in Strategy Lab/ });

    expect(screen.getByText('Harness settings')).toBeTruthy();
    expect(link.getAttribute('href')).toContain('engine=python');
    expect(link.getAttribute('href')).not.toContain('engine=both');
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
    expect(screen.getAllByText('Proof missing').length).toBeGreaterThan(0);
    expect(screen.getByText('Validation evidence has not been registered yet.')).toBeTruthy();
  });

  it('shows the SPY EMA QuantConnect audit copy even before validation evidence exists', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /EMA Crossover Signal/ }));

    expect(
      await screen.findByRole('heading', { name: 'QuantConnect reference algorithm', level: 3 }),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy QuantConnect algorithm' })).toBeTruthy();
    expect(screen.getByText('references/qc-shadow/SpyEmaCrossoverAlgorithm.py')).toBeTruthy();
  });

  it('shows a retryable load error instead of an empty EMA detail pane', async () => {
    const service = new FakeStrategyValidationService();
    let emaDetailUnavailable = true;
    service.getDetail.mockImplementation((key: string) =>
      key === 'ema_crossover_signal' && emaDetailUnavailable
        ? Promise.reject(new Error('Strategy audit copy unreadable'))
        : Promise.resolve(DETAIL_BY_KEY[key] ?? ORB_DETAIL),
    );
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /EMA Crossover Signal/ }));

    expect((await screen.findByRole('alert')).textContent).toContain('Validation evidence could not be loaded.');
    emaDetailUnavailable = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('heading', { name: 'EMA Crossover Signal' })).toBeTruthy();
    expect(service.getDetail).toHaveBeenCalledWith('ema_crossover_signal');
  });

  it('opens the requested strategy audit copy from a Strategy Lab link', async () => {
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { queryParamMap: of(convertToParamMap({ strategy: 'ema_crossover_signal' })) },
        },
        { provide: StrategyValidationService, useClass: FakeStrategyValidationService },
      ],
    });

    expect(await screen.findByRole('heading', { name: 'EMA Crossover Signal' })).toBeTruthy();
    expect(
      screen.getByRole('heading', { name: 'QuantConnect reference algorithm', level: 3 }),
    ).toBeTruthy();
  });

  it('refreshes validation evidence for the selected strategy', async () => {
    const service = new FakeStrategyValidationService();
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Recheck stored proof' }));

    await waitFor(() => {
      expect(service.refreshValidationEvidence).toHaveBeenCalledWith('deployment_validation');
    });
    expect(await screen.findByText(/Stored proof rechecked/)).toBeTruthy();
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

  it('records an operational harness review without requiring a QuantConnect run', async () => {
    const service = new FakeStrategyValidationService();
    await render(StrategyValidationComponent, {
      providers: [
        provideRouter([]),
        { provide: StrategyValidationService, useValue: service },
      ],
    });

    expect(screen.queryByLabelText('QC Cloud backtest ID')).toBeNull();
    fireEvent.input(await screen.findByLabelText('Reason'), {
      target: { value: 'Internal harness qualification reviewed.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));

    await waitFor(() => {
      expect(service.flagValidation).toHaveBeenCalledWith('deployment_validation', {
        flag: 'validated',
        reason: 'Internal harness qualification reviewed.',
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
