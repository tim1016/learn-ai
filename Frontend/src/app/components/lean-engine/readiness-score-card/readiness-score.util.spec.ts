import fixtureJson from '@repo-contracts/run-verdict-v1/fixture.json';
import { computeReadiness, type ReadinessEngine, type ReadinessResultLike } from './readiness-score.util';

interface FixtureCase {
  id: string;
  engine: ReadinessEngine;
  input: ReadinessResultLike;
  expected: {
    composite: number | null;
    grade: string | null;
    signal: string | null;
    headline: string;
    missing_metrics_count: number;
    normalized_weights: boolean;
    dimension_scores: Record<string, number | null>;
    sub_scores: Record<string, Record<string, number | null>>;
  };
}

const fixture = fixtureJson as { cases: FixtureCase[] };

function dimensionScores(report: ReturnType<typeof computeReadiness>): Record<string, number | null> {
  return Object.fromEntries(report.dimensions.map((dimension) => [dimension.key, dimension.score]));
}

function subScores(report: ReturnType<typeof computeReadiness>): Record<string, Record<string, number | null>> {
  return Object.fromEntries(
    report.dimensions.map((dimension) => [
      dimension.key,
      Object.fromEntries(dimension.subScores.map((subScore) => [subScore.key, subScore.score])),
    ]),
  );
}

describe('computeReadiness golden parity', () => {
  it.each(fixture.cases)('matches run verdict v1 fixture: $id', (testCase) => {
    const report = computeReadiness(testCase.input, testCase.engine);

    expect(report.composite).toEqual(testCase.expected.composite);
    expect(report.grade).toEqual(testCase.expected.grade);
    expect(report.signal).toEqual(testCase.expected.signal);
    expect(report.verdict).toEqual(testCase.expected.headline);
    expect(report.missingMetrics).toHaveLength(testCase.expected.missing_metrics_count);
    expect(report.normalizedWeights).toEqual(testCase.expected.normalized_weights);
    expect(dimensionScores(report)).toEqual(testCase.expected.dimension_scores);
    expect(subScores(report)).toEqual(testCase.expected.sub_scores);
  });

  it('grades infinite profit factor as a longer-window warning', () => {
    const report = computeReadiness(
      {
        statistics: { profit_factor: Number.POSITIVE_INFINITY },
        win_rate: null,
        total_trades: 30,
        net_profit: 1000,
        total_fees: 50,
        lean_statistics: null,
      },
      'python',
    );

    const tradeEdge = report.dimensions.find((dimension) => dimension.key === 'trade_edge');
    const profitFactor = tradeEdge?.subScores.find((subScore) => subScore.key === 'pf');
    expect(profitFactor?.score).toBe(10);
    expect(profitFactor?.display).toBe('∞');
  });
});
