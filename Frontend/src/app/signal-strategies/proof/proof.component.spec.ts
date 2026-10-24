import { TestBed } from '@angular/core/testing';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  StrategyProofDossier,
  StrategyProofStage,
  StrategyValidationDetail,
  StrategyValidationFlagEvent,
} from '../../services/strategy-validation.types';
import { harnessEntry, proofRecord, registryEntry } from '../testing/fixtures';
import { emptyWorld, provideSignalStrategyWorld, type SignalStrategyServices, type SignalStrategyWorld } from '../testing/signal-strategy-world';

const EMA = 'ema_crossover_signal';
const HARNESS = 'deployment_validation';

afterEach(() => {
  TestBed.resetTestingModule();
});

function stage(stageId: string, title: string, state: StrategyProofStage['state']): StrategyProofStage {
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

const HARNESS_PROOF: StrategyProofDossier = {
  state: 'current',
  completed_stages: 3,
  total_stages: 3,
  blocking_stage_id: null,
  blocking_summary: null,
  stages: [
    stage('program_contract', 'Signal Program contract', 'complete'),
    stage('reference_run', 'QuantConnect reference run', 'not_applicable'),
    stage('current_proof', 'Current validation proof', 'complete'),
  ],
};

const EMA_PROOF: StrategyProofDossier = {
  state: 'missing',
  completed_stages: 1,
  total_stages: 2,
  blocking_stage_id: 'reference_run',
  blocking_summary: 'Run the QuantConnect reference backtest.',
  stages: [stage('reference_source', 'QuantConnect reference algorithm', 'complete'), stage('reference_run', 'QuantConnect reference run', 'missing')],
};

const ACCEPTED: StrategyValidationFlagEvent = {
  event_id: 'seed-accepted',
  strategy_key: HARNESS,
  flag: 'validated',
  flagged_by: 'migration:strategy-validation-prd-seed',
  flagged_at_ms: 1_775_088_000_000,
  reason: 'Accepted for deployment.',
  behavioral_equivalence: { verdict: 'accepted_for_deploy', detail: 'Human validation accepted the current engine evidence for deployment.' },
  evidence_snapshot: {
    validator_code_ref: 'app/lean_sidecar/trusted_samples/deployment_validation.py',
    validator_code_sha256: 'validator-sha',
    settings_file_ref: 'app/engine/strategy/spec/fixtures/deployment_validation.spec.json',
    settings_file_sha256: 'spec-sha',
    qc_cloud_backtest_id: null,
    audit_copy_ref: null,
    audit_copy_sha256: null,
    reconciliation_ref: null,
    validation_case_symbol: 'SPY',
    reconciliation_status: 'passed',
    diagnostics: null,
  },
  evidence_snapshot_sha256: 'snapshot-sha',
  superseded_by_event_id: null,
};

const HARNESS_DETAIL: StrategyValidationDetail = {
  strategy_key: HARNESS,
  display_name: 'Deployment Validation',
  description: 'Two-green-minute deployment validation primitive.',
  strategy_category: 'operational_validation_harness',
  validation_state: 'validated',
  deployable: true,
  proof: HARNESS_PROOF,
  validator_code_ref: 'app/lean_sidecar/trusted_samples/deployment_validation.py',
  validator_code_sha256: 'validator-sha',
  settings_file_ref: 'app/engine/strategy/spec/fixtures/deployment_validation.spec.json',
  settings_file_sha256: 'spec-sha',
  qc_cloud_backtest_id: null,
  audit_copy_ref: null,
  audit_copy_sha256: null,
  reconciliation_ref: null,
  validation_case_symbol: 'SPY',
  reconciliation_status: 'passed',
  diagnostics: { verdict: 'passed', trades_matched: 56, trades_validated: 56, pnl_max_abs_diff: '0.00', divergence_counts: { fill_price_drift: 2 }, notes: [] },
  behavioral_equivalence: ACCEPTED.behavioral_equivalence,
  current_flag_event: ACCEPTED,
  flag_events: [ACCEPTED],
  reference_code: null,
};

const EMA_DETAIL: StrategyValidationDetail = {
  strategy_key: EMA,
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

function detailService() {
  const byKey: Record<string, StrategyValidationDetail> = { [EMA]: EMA_DETAIL, [HARNESS]: HARNESS_DETAIL };
  return {
    getDetail: vi.fn(async (key: string) => byKey[key]),
    refreshValidationEvidence: vi.fn(async (key: string) => ({ refresh_id: `manifest-evidence:${key}:123`, refreshed_at_ms: 123, detail: byKey[key] })),
    flagValidation: vi.fn(async (key: string) => ({ ...byKey[key], current_flag_event: { ...ACCEPTED, flag: 'invalidated' as const } })),
  };
}

async function open(url: string, world: SignalStrategyWorld, validation: Record<string, unknown> = detailService()) {
  const services: Partial<SignalStrategyServices> = { validation };
  TestBed.configureTestingModule({ providers: provideSignalStrategyWorld(world, {}, services) });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return harness;
}

const proofWorld = (overrides: Partial<SignalStrategyWorld> = {}) =>
  emptyWorld({
    entries: [registryEntry(), harnessEntry()],
    proof: [
      proofRecord(EMA, 'missing', { validation_state: 'needs_validation', deployable: false, proof: { state: 'missing', completed_stages: 1, total_stages: 2 } }),
      proofRecord(HARNESS, 'current'),
    ],
    ...overrides,
  });

describe('a strategy’s Proof', () => {
  it('shows a strategy the proof catalog holds no record for as having none, and approves nothing (T11)', async () => {
    const service = detailService();
    await open(`/signal-strategies/${EMA}/proof`, proofWorld({ proof: [proofRecord(HARNESS, 'current')] }), service);

    expect(await screen.findByText(/holds no record for this strategy, so nothing about its proof is known/)).toBeTruthy();
    expect(service.getDetail).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Program proof decision' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save flag' })).toBeNull();
  });

  it('says a proof catalog that could not be read is unknown, with a way to read it again', async () => {
    await open(`/signal-strategies/${EMA}/proof`, proofWorld({ proof: new Error('catalog down') }));

    expect(await screen.findByText('Program proof could not be read.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Read it again' })).toBeTruthy();
    expect(screen.queryByText(/holds no record/)).toBeNull();
  });

  it('shows the operational harness as a non-Live harness, with its proof pipeline, its accepted flag and the evidence behind it (T11)', async () => {
    await open(`/signal-strategies/${HARNESS}/proof`, proofWorld());

    expect(await screen.findByRole('heading', { name: 'Operational validation harness' })).toBeTruthy();
    expect(screen.getByText(/permanently ineligible for Live/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Strategy proof' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Current validation proof' })).toBeTruthy();
    expect(screen.getByText('Program proof accepted for a Paper test')).toBeTruthy();
    expect(screen.getByText('migration:strategy-validation-prd-seed')).toBeTruthy();
    expect(screen.getByText('snapshot-sha')).toBeTruthy();
    expect(screen.getByText('56 trades matched')).toBeTruthy();
    expect(screen.getByText('Fill Price Drift')).toBeTruthy();
    expect(screen.queryByText('fill_price_drift')).toBeNull();
    // A harness is checked against an internal replay, so it asks for no QuantConnect run.
    expect(screen.queryByLabelText('QC Cloud backtest ID')).toBeNull();
  });

  it('keeps Diagnose in Backtest and the accepted proof’s Deploy link, and says the proof approves no symbol’s settings', async () => {
    await open(`/signal-strategies/${HARNESS}/proof`, proofWorld());

    const diagnose = await screen.findByRole('link', { name: /Diagnose in Backtest/ });
    expect(diagnose.getAttribute('href')).toContain(`/signal-strategies/${HARNESS}/explore/backtest?`);
    expect(diagnose.getAttribute('href')).toContain('engine=both');
    expect(diagnose.getAttribute('href')).toContain('symbol=SPY');
    expect(screen.getByRole('link', { name: /Deploy strategy/ }).getAttribute('href')).toBe(`/brokers/alpaca?deploy=&strategy=${HARNESS}`);
    const scope = screen.getByText(/It approves no symbol's settings/);
    expect(within(scope).getByRole('link', { name: 'Golden' }).getAttribute('href')).toBe(`/signal-strategies/${HARNESS}/golden/configuration`);
  });

  it('does not treat the deploy binding as a LEAN validator when validator evidence is missing', async () => {
    const service = detailService();
    service.getDetail.mockResolvedValue({ ...HARNESS_DETAIL, validator_code_ref: null, validator_code_sha256: null });
    await open(`/signal-strategies/${HARNESS}/proof`, proofWorld(), service);

    const link = await screen.findByRole('link', { name: /Diagnose in Backtest/ });
    expect(screen.getByText('Harness settings')).toBeTruthy();
    expect(link.getAttribute('href')).toContain('engine=python');
    expect(link.getAttribute('href')).not.toContain('engine=both');
  });

  it('shows a production candidate that is not accepted as not accepted, with its blocking step and no Deploy link', async () => {
    await open(`/signal-strategies/${EMA}/proof`, proofWorld());

    expect(await screen.findByText('Not accepted for Deploy yet')).toBeTruthy();
    const decision = screen.getByRole('region', { name: 'Program proof decision' });
    expect(within(decision).getByText('Run the QuantConnect reference backtest.')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'QuantConnect reference algorithm', level: 3 })).toBeTruthy();
    expect(screen.getByText('Validation evidence has not been registered yet.')).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Deploy strategy/ })).toBeNull();
  });

  it('says what could not be loaded, and loads it again on request, instead of leaving an empty pane', async () => {
    const service = detailService();
    let down = true;
    service.getDetail.mockImplementation(async (key: string) => {
      if (down) throw new Error('Strategy audit copy unreadable');
      return key === EMA ? EMA_DETAIL : HARNESS_DETAIL;
    });
    await open(`/signal-strategies/${EMA}/proof`, proofWorld(), service);

    expect(await screen.findByText('Validation evidence could not be loaded.')).toBeTruthy();
    expect(screen.getByText('Strategy audit copy unreadable')).toBeTruthy();
    down = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('heading', { name: 'EMA Crossover Signal' })).toBeTruthy();
    expect(service.getDetail).toHaveBeenCalledWith(EMA);
  });

  it('rechecks the stored proof for this strategy and reads the proof again', async () => {
    const service = detailService();
    await open(`/signal-strategies/${HARNESS}/proof`, proofWorld(), service);

    fireEvent.click(await screen.findByRole('button', { name: 'Recheck stored proof' }));

    await waitFor(() => expect(service.refreshValidationEvidence).toHaveBeenCalledWith(HARNESS));
    expect(await screen.findByText(/Stored proof rechecked from/)).toBeTruthy();
    await waitFor(() => expect(service.getDetail).toHaveBeenCalledTimes(2));
  });

  it('requires a reason before saving the human flag, then saves the one chosen', async () => {
    const service = detailService();
    await open(`/signal-strategies/${HARNESS}/proof`, proofWorld(), service);

    fireEvent.click(await screen.findByRole('button', { name: 'Save flag' }));
    expect((await screen.findByRole('alert')).textContent).toContain('A validation reason is required.');
    expect(service.flagValidation).not.toHaveBeenCalled();

    fireEvent.click(screen.getByLabelText('Reject'));
    fireEvent.input(screen.getByLabelText('Reason'), { target: { value: 'Reject this evidence.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));

    await waitFor(() => expect(service.flagValidation).toHaveBeenCalledWith(HARNESS, { flag: 'invalidated', reason: 'Reject this evidence.' }));
    expect(await screen.findByText('Rejected flag saved.')).toBeTruthy();
  });

  it('records a harness review without a QuantConnect run, and requires one to accept a production candidate', async () => {
    const service = detailService();
    const harness = await open(`/signal-strategies/${HARNESS}/proof`, proofWorld(), service);

    fireEvent.input(await screen.findByLabelText('Reason'), { target: { value: 'Internal harness qualification reviewed.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));
    await waitFor(() => expect(service.flagValidation).toHaveBeenCalledWith(HARNESS, { flag: 'validated', reason: 'Internal harness qualification reviewed.' }));

    await harness.navigateByUrl(`/signal-strategies/${EMA}/proof`);
    fireEvent.input(await screen.findByLabelText('Reason'), { target: { value: 'Trades match within the accepted gate.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));
    expect((await screen.findByRole('alert')).textContent).toContain('A QC Cloud backtest ID is required to accept validation evidence.');

    fireEvent.input(screen.getByLabelText('QC Cloud backtest ID'), { target: { value: 'qc-backtest-42' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save flag' }));
    await waitFor(() =>
      expect(service.flagValidation).toHaveBeenCalledWith(EMA, { flag: 'validated', reason: 'Trades match within the accepted gate.', qc_cloud_backtest_id: 'qc-backtest-42' }),
    );
  });
});
