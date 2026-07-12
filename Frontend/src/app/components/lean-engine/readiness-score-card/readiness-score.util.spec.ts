import { computeReadiness, type ReadinessResultLike } from './readiness-score.util';

interface FixtureCase {
  id: string;
  engine: 'python' | 'lean';
  input: ReadinessResultLike;
  expected: {
    composite: number | null;
    grade: string | null;
    signal: string | null;
    headline: string;
    missing_metrics_count: number;
    normalized_weights: boolean;
    dimension_scores: Record<string, number | null>;
    trade_gap_score: number | null;
  };
}

const cases: FixtureCase[] = [
  {
    id: 'strong_python',
    engine: 'python',
    input: {
      statistics: {
        sharpe_ratio: 1.7,
        sortino_ratio: 2.2,
        max_drawdown_pct: 0.06,
        profit_factor: 2.2,
        expectancy_pct: 0.004,
      },
      win_rate: 0.62,
      total_trades: 180,
      net_profit: 22000,
      total_fees: 800,
      lean_statistics: {
        portfolio: {
          compounding_annual_return: 0.18,
          annual_standard_deviation: 0.12,
          probabilistic_sharpe_ratio: 0.97,
          drawdown_recovery: 18,
          sharpe_ratio: 1.7,
          sortino_ratio: 2.2,
        },
        trade: {
          profit_factor: 2.2,
          sharpe_ratio: 1.9,
          max_consecutive_losing_trades: 4,
          average_profit: 350,
          average_loss: -180,
        },
      },
    },
    expected: {
      composite: 90,
      grade: 'A+',
      signal: 'Deploy',
      headline: 'Institutional-grade. Ready for live deployment at standard size. 8 sub-scores unavailable; grade may move once missing metrics are computed.',
      missing_metrics_count: 8,
      normalized_weights: true,
      dimension_scores: {
        return_quality: 89,
        risk_control: 83,
        trade_edge: 92,
        stat_confidence: 98,
        alpha_calibration: null,
      },
      trade_gap_score: 20,
    },
  },
  {
    id: 'suspicious_edges',
    engine: 'python',
    input: {
      statistics: {
        sharpe_ratio: 3.01,
        sortino_ratio: 4.1,
        max_drawdown_pct: 0.01,
        profit_factor: 4.01,
        expectancy_pct: 0.03,
      },
      win_rate: 0.86,
      total_trades: 42,
      net_profit: 12000,
      total_fees: 200,
      lean_statistics: {
        portfolio: {
          compounding_annual_return: 0.31,
          annual_standard_deviation: 0.025,
          probabilistic_sharpe_ratio: 0.995,
          drawdown_recovery: 5,
          sharpe_ratio: 3.01,
          sortino_ratio: 4.1,
        },
        trade: {
          profit_factor: 4.01,
          sharpe_ratio: 8.2,
          max_consecutive_losing_trades: 1,
          average_profit: 500,
          average_loss: -100,
        },
      },
    },
    expected: {
      composite: 69,
      grade: 'B',
      signal: 'Iterate',
      headline: 'Promising edge, but specific weaknesses need addressing before deployment. 8 sub-scores unavailable; grade may move once missing metrics are computed.',
      missing_metrics_count: 8,
      normalized_weights: true,
      dimension_scores: {
        return_quality: 74,
        risk_control: 95,
        trade_edge: 70,
        stat_confidence: 34,
        alpha_calibration: null,
      },
      trade_gap_score: 2,
    },
  },
  {
    id: 'weak_python',
    engine: 'python',
    input: {
      statistics: {
        sharpe_ratio: -0.2,
        sortino_ratio: 0.4,
        max_drawdown_pct: 0.35,
        profit_factor: 0.8,
        expectancy_pct: -0.002,
      },
      win_rate: 0.25,
      total_trades: 12,
      net_profit: -5000,
      total_fees: 800,
      lean_statistics: {
        portfolio: {
          compounding_annual_return: -0.05,
          annual_standard_deviation: 0.4,
          probabilistic_sharpe_ratio: 0.4,
          drawdown_recovery: 300,
          sharpe_ratio: -0.2,
          sortino_ratio: 0.4,
        },
        trade: {
          profit_factor: 0.8,
          sharpe_ratio: -0.1,
          max_consecutive_losing_trades: 15,
          average_profit: 100,
          average_loss: -260,
        },
      },
    },
    expected: {
      composite: 17,
      grade: 'F',
      signal: 'Reject',
      headline: 'Reject — the backtest does not clear baseline viability. 8 sub-scores unavailable; grade may move once missing metrics are computed.',
      missing_metrics_count: 8,
      normalized_weights: true,
      dimension_scores: {
        return_quality: 6,
        risk_control: 2,
        trade_edge: 8,
        stat_confidence: 55,
        alpha_calibration: null,
      },
      trade_gap_score: 20,
    },
  },
  {
    id: 'all_missing',
    engine: 'python',
    input: {
      statistics: {},
      win_rate: null,
      total_trades: null,
      net_profit: null,
      total_fees: null,
      lean_statistics: null,
    },
    expected: {
      composite: null,
      grade: null,
      signal: null,
      headline: 'Not enough data to grade.',
      missing_metrics_count: 25,
      normalized_weights: false,
      dimension_scores: {
        return_quality: null,
        risk_control: null,
        trade_edge: null,
        stat_confidence: null,
        alpha_calibration: null,
      },
      trade_gap_score: null,
    },
  },
];

function dimensionScores(report: ReturnType<typeof computeReadiness>): Record<string, number | null> {
  return Object.fromEntries(report.dimensions.map((dimension) => [dimension.key, dimension.score]));
}

function tradeGapScore(report: ReturnType<typeof computeReadiness>): number | null {
  const statConfidence = report.dimensions.find((dimension) => dimension.key === 'stat_confidence');
  const tradeGap = statConfidence?.subScores.find((subScore) => subScore.key === 'trade_gap');
  return tradeGap?.score ?? null;
}

describe('computeReadiness golden parity', () => {
  it.each(cases)('matches run verdict v1 fixture: $id', (testCase) => {
    const report = computeReadiness(testCase.input);

    expect(report.composite).toEqual(testCase.expected.composite);
    expect(report.grade).toEqual(testCase.expected.grade);
    expect(report.signal).toEqual(testCase.expected.signal);
    expect(report.verdict).toEqual(testCase.expected.headline);
    expect(report.missingMetrics).toHaveLength(testCase.expected.missing_metrics_count);
    expect(report.normalizedWeights).toEqual(testCase.expected.normalized_weights);
    expect(dimensionScores(report)).toEqual(testCase.expected.dimension_scores);
    expect(tradeGapScore(report)).toEqual(testCase.expected.trade_gap_score);
  });
});
