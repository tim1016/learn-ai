import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { ResearchService } from './research.service';
import { environment } from '../../environments/environment';

const BASE = `${environment.pythonServiceUrl}/api/research`;

/** A report with every optional block present, so each mapper has something to read. */
const FULL_REPORT = {
  success: true,
  ticker: 'AAPL',
  feature_name: 'momentum_5m',
  start_date: '2024-01-01',
  end_date: '2024-03-31',
  bars_used: 200,
  flip_sign: true,
  thresholds_tested: [0.5, 1.0],
  cost_bps_options: [2, 5],
  best_threshold: 1.0,
  best_cost_bps: 5,
  backtest_grid: [{
    threshold: 1.0, cost_bps: 5, dates: ['2024-01-02'], cumulative_returns: [0.01], positions: [1],
    gross_sharpe: 1.1, net_sharpe: 0.9, max_drawdown: -0.05, annualized_turnover: 12, avg_holding_bars: 3,
    win_rate: 0.55, avg_win_loss_ratio: 1.2, total_trades: 40, net_total_return: 0.08, gross_total_return: 0.1,
  }],
  walk_forward: {
    windows: [{
      fold_index: 0, train_start: '2024-01-01', train_end: '2024-02-01', test_start: '2024-02-02', test_end: '2024-03-01',
      train_bars: 100, test_bars: 40, mu: 0.1, sigma: 0.2, best_threshold: 1.0,
      oos_net_sharpe: 0.7, oos_gross_sharpe: 0.9, oos_max_drawdown: -0.03, oos_net_return: 0.02,
      oos_win_rate: 0.5, oos_total_trades: 9, oos_dates: ['2024-02-02'], oos_cumulative_returns: [0.001],
    }],
    mean_oos_sharpe: 0.8, std_oos_sharpe: 0.1, median_oos_sharpe: 0.75, pct_windows_profitable: 0.6,
    pct_windows_positive_sharpe: 0.7, worst_window_sharpe: -0.2, best_window_sharpe: 1.4, total_oos_bars: 120,
    combined_oos_dates: ['2024-02-02'], combined_oos_cumulative_returns: [0.001], oos_sharpe_trend_slope: -0.01,
    alpha_decay: {
      slope: -0.1, intercept: 0.5, t_stat: -1.2, p_value: 0.3, r_squared: 0.2, n_folds_used: 6,
      is_test_valid: true, is_significant: false,
    },
  },
  graduation: {
    criteria: [{
      name: 'oos_sharpe', description: 'OOS Sharpe', passed: true, value: 0.8, threshold: 0.5, label: 'Pass', failure_reason: '',
    }],
    overall_passed: true, overall_grade: 'B', summary: 'ok', status_label: 'Candidate',
    parameter_stability: {
      sharpe_values_by_threshold: [{ threshold: 0.5, sharpe: 0.6 }, { threshold: 1.0, sharpe: 0.8 }],
      stability_score: 0.9, stability_label: 'Stable',
    },
    stage0_rejection: {
      rejected: false,
      failed_criteria: [{ criterion_name: 'min_bars', value: 50, threshold_repr: '>= 100', message: 'too few bars' }],
    },
    stage_info: {
      stage: 2, label: 'Research Candidate', description: 'd', next_stage_label: 'Promotion Candidate',
      advance_criteria: [{ name: 'dsr', description: 'DSR', current_value: 0.4, required_repr: '>= 0.95', met: false }],
    },
  },
  signal_diagnostics: {
    signal_mean: 0, signal_std: 1, pct_time_active: 0.4, avg_abs_signal: 0.8,
    pct_filtered_by_threshold: 0.3, pct_gated_by_regime: 0.1,
  },
  data_sufficiency: {
    total_bars: 200, train_bars: 120, test_bars: 80, walk_forward_folds: 6, effective_oos_bars: 70, regimes_covered: 2,
    regime_coverage: [{ regime: 'low_vol', count: 120 }, { regime: 'high_vol', count: 80 }],
    coverage_warnings: ['few high-vol bars'],
  },
  effective_sample: {
    raw_n: 200, effective_n: 150, autocorrelation_lag1: 0.2, independent_bets: 30, max_lag_used: 5, rho_sum: 0.3,
  },
  regime_coverage: [{ regime: 'low_vol', count: 120 }],
  joint_regime_coverage: [{ vol_label: 'low', trend_label: 'up', days: 20, effective_trades: 6, badge: 'Pass' }],
  signal_behavior: {
    avg_forward_return_when_active: 0.001, skewness_active_returns: 0.1, avg_win_return: 0.01, avg_loss_return: -0.008, hit_rate: 0.52,
  },
  oos_sharpe_ci: { point: 0.8, se: 0.2, ci_lower: 0.4, ci_upper: 1.2, confidence_level: 0.95, n_eff_used: 150, valid: true },
  deflated_sharpe: {
    raw_sharpe: 1.1, expected_max_under_null: 0.6, dsr_probability: 0.9, n_trials: 10, skewness: 0.1, kurtosis: 3, valid: true,
  },
  methodology: {
    train_months: 6, test_months: 1, window_type: 'rolling', optimization_target: 'net_sharpe', annualization_factor: 252,
    bars_per_day: 26, horizon: 1, default_cost_bps: 5, min_bars_for_signal: 100, flip_sign: true, regime_gate_enabled: true,
    thresholds: [0.5, 1.0], cost_bps_options: [2, 5],
  },
  research_log: 'log',
  error: null,
};

const MISSING = { detail: { code: 'SIGNAL_EXPERIMENT_NOT_FOUND', message: 'Signal experiment 999 not found' } };

describe('ResearchService', () => {
  let service: ResearchService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(ResearchService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  describe('getSignalExperimentReport', () => {
    it('maps every block of the stored report, keeping the regime and threshold entry lists', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(42));

      const req = httpMock.expectOne(`${BASE}/signal-experiments/42/report`);
      expect(req.request.method).toBe('GET');
      req.flush(FULL_REPORT);

      const result = await pending;
      if (!result) throw new Error('Expected a signal experiment report');
      expect.soft(result.featureName).toBe('momentum_5m');
      expect.soft(result.thresholdsTested).toEqual([0.5, 1.0]);
      expect.soft(result.error).toBeUndefined();
      expect.soft(result.backtestGrid[0]).toMatchObject({ costBps: 5, netSharpe: 0.9, totalTrades: 40 });
      expect.soft(result.walkForward?.windows[0]).toMatchObject({ foldIndex: 0, oosNetSharpe: 0.7, trainStart: '2024-01-01' });
      expect.soft(result.walkForward?.alphaDecay).toMatchObject({ tStat: -1.2, nFoldsUsed: 6, isTestValid: true });
      expect.soft(result.graduation?.parameterStability?.sharpeValuesByThreshold).toEqual([
        { threshold: 0.5, sharpe: 0.6 }, { threshold: 1.0, sharpe: 0.8 },
      ]);
      expect.soft(result.graduation?.stage0Rejection?.failedCriteria[0]).toEqual({
        criterionName: 'min_bars', value: 50, thresholdRepr: '>= 100', message: 'too few bars',
      });
      expect.soft(result.graduation?.stageInfo).toMatchObject({ stage: 2, nextStageLabel: 'Promotion Candidate' });
      expect.soft(result.dataSufficiency?.regimeCoverage).toEqual([
        { regime: 'low_vol', count: 120 }, { regime: 'high_vol', count: 80 },
      ]);
      expect.soft(result.regimeCoverage).toEqual([{ regime: 'low_vol', count: 120 }]);
      expect.soft(result.jointRegimeCoverage[0]).toEqual({
        volLabel: 'low', trendLabel: 'up', days: 20, effectiveTrades: 6, badge: 'Pass',
      });
      expect.soft(result.effectiveSample).toMatchObject({ rawN: 200, autocorrelationLag1: 0.2 });
      expect.soft(result.signalBehavior).toMatchObject({ avgForwardReturnWhenActive: 0.001, hitRate: 0.52 });
      expect.soft(result.oosSharpeCi).toMatchObject({ ciLower: 0.4, ciUpper: 1.2, nEffUsed: 150, method: 'legacy_neff' });
      expect.soft(result.deflatedSharpe).toMatchObject({ dsrProbability: 0.9, nTrials: 10, method: 'legacy_proxy' });
      expect.soft(result.methodology).toMatchObject({ trainMonths: 6, thresholds: [0.5, 1.0], costBpsOptions: [2, 5] });
    });

    it('preserves new inference methods and unavailable reasons', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(44));
      httpMock.expectOne(`${BASE}/signal-experiments/44/report`).flush({
        ...FULL_REPORT,
        oos_sharpe_ci: { ...FULL_REPORT.oos_sharpe_ci, method: 'lo_hac', sample_size: 1200, hac_lag: 14, segment_count: 4 },
        deflated_sharpe: { ...FULL_REPORT.deflated_sharpe, method: 'dsr_grid', valid: false, sample_size: 800,
          effective_trials: 1.4, trial_sharpe_std: 0.01, mean_trial_correlation: 0.9,
          unavailable_reason: 'Effective trial count between one and two' },
      });
      const result = await pending;
      expect(result?.oosSharpeCi).toMatchObject({ method: 'lo_hac', sampleSize: 1200, hacLag: 14, segmentCount: 4 });
      expect(result?.deflatedSharpe).toMatchObject({ method: 'dsr_grid', valid: false, sampleSize: 800,
        effectiveTrials: 1.4, trialSharpeStd: 0.01, meanTrialCorrelation: 0.9,
        unavailableReason: 'Effective trial count between one and two' });
    });

    it('maps absent blocks to null and a stored error to the view model\'s error', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(43));

      httpMock.expectOne(`${BASE}/signal-experiments/43/report`).flush({
        ...FULL_REPORT,
        success: false, error: 'engine failed', backtest_grid: [],
        walk_forward: null, graduation: null, signal_diagnostics: null, data_sufficiency: null,
        effective_sample: null, signal_behavior: null, oos_sharpe_ci: null, deflated_sharpe: null, methodology: null,
        regime_coverage: [], joint_regime_coverage: [],
      });

      const result = await pending;
      expect(result).toMatchObject({
        success: false, error: 'engine failed', walkForward: null, graduation: null, methodology: null, regimeCoverage: [],
      });
    });

    it('resolves null for an experiment nobody holds, so the page says "not found"', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(999));

      httpMock.expectOne(`${BASE}/signal-experiments/999/report`).flush(MISSING, { status: 404, statusText: 'Not Found' });

      await expect(pending).resolves.toBeNull();
    });

    it('does not mistake a missing route for a missing experiment', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(999));

      httpMock.expectOne(`${BASE}/signal-experiments/999/report`).flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });

      await expect(pending).rejects.toThrow('Could not load the signal report: Not Found');
    });

    it('rejects with Python\'s sentence for a stored report it cannot read', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(42));

      httpMock.expectOne(`${BASE}/signal-experiments/42/report`).flush(
        { detail: { code: 'SIGNAL_REPORT_UNREADABLE', message: 'The stored report of signal experiment 42 cannot be read: bad JSON' } },
        { status: 500, statusText: 'Internal Server Error' },
      );

      await expect(pending).rejects.toThrow(
        'Could not load the signal report: The stored report of signal experiment 42 cannot be read: bad JSON',
      );
    });

    it('rejects a report that names a graduation stage off the ladder instead of guessing a rung', async () => {
      const pending = firstValueFrom(service.getSignalExperimentReport(44));
      const stageInfo = { ...FULL_REPORT.graduation.stage_info, stage: 7 };

      httpMock.expectOne(`${BASE}/signal-experiments/44/report`).flush({
        ...FULL_REPORT,
        graduation: { ...FULL_REPORT.graduation, stage_info: stageInfo },
      });

      await expect(pending).rejects.toThrow('graduation stage 7');
    });
  });
});
