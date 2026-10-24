import { buildChartRequestBody } from '../../components/data-lab/data-lab-request-mapper';
import type { DataLabDraftScope, DataLabIndicatorInstance } from '../../components/data-lab/data-lab-workspace-store';

/**
 * The bar timeframe a committed scope asks the chart endpoint for, in that
 * endpoint's vocabulary (`1D`, `5m`, `1h`, `1W`, `1M`).
 */
export function chartTimeframeOf(scope: Pick<DataLabDraftScope, 'timespan' | 'multiplier'>): string {
  if (scope.timespan === 'minute') return `${scope.multiplier}m`;
  if (scope.timespan === 'hour') return `${scope.multiplier}h`;
  if (scope.timespan === 'day') return scope.multiplier === 1 ? '1D' : `${scope.multiplier}D`;
  if (scope.timespan === 'week') return `${scope.multiplier}W`;
  if (scope.timespan === 'month') return `${scope.multiplier}M`;
  return '1D';
}

/**
 * The request a committed scope and recipe make, as the text that identifies
 * it: the whole body the chart endpoint is posted. Two requests that post the
 * same body are the same request, so the Chart tab and a saved setup that
 * holds the answer to one name it the same way.
 */
export function chartRequestIdentity(
  scope: DataLabDraftScope,
  indicators: readonly DataLabIndicatorInstance[],
): string {
  return JSON.stringify(
    buildChartRequestBody({
      ticker: scope.ticker,
      window: scope.window,
      timeframe: chartTimeframeOf(scope),
      session: scope.session,
      forwardFill: scope.forwardFill,
      adjusted: scope.adjusted,
      indicators,
      computeAllIndicators: false,
    }),
  );
}
