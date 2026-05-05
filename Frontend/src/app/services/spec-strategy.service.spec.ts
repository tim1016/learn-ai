import { TestBed } from '@angular/core/testing';
import { ApolloTestingController, ApolloTestingModule } from 'apollo-angular/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { StrategySpec } from '../graphql/spec-strategy-types';
import { RUN_SPEC_STRATEGY_BACKTEST, SpecStrategyService } from './spec-strategy.service';

const TRIVIAL_SPEC: StrategySpec = {
  schema_version: '1.0',
  name: 'spec-service-test',
  symbols: ['SPY'],
  resolution: { period_minutes: 15 },
  indicators: [
    { id: 'sma_s', kind: 'SMA', period: 5 },
    { id: 'sma_l', kind: 'SMA', period: 10 },
  ],
  entry: {
    logic: 'AND',
    conditions: [{ kind: 'FreshCross', left: 'sma_s', right: 'sma_l', direction: 'up' }],
    size: { kind: 'SetHoldings', fraction: 1.0 },
  },
  exit: { logic: 'OR', conditions: [] },
};

describe('SpecStrategyService', () => {
  let service: SpecStrategyService;
  let controller: ApolloTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ApolloTestingModule],
    });
    service = TestBed.inject(SpecStrategyService);
    controller = TestBed.inject(ApolloTestingController);
  });

  afterEach(() => {
    controller.verify();
  });

  it('serializes the spec to JSON and forwards run params to the mutation', async () => {
    const promise = service.runBacktest(TRIVIAL_SPEC, {
      startDate: '2024-01-02',
      endDate: '2024-12-31',
      initialCash: 50000,
      fillMode: 'signal_bar_close',
      commissionPerOrder: 0,
    });

    const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);

    expect(op.operation.variables['startDate']).toBe('2024-01-02');
    expect(op.operation.variables['endDate']).toBe('2024-12-31');
    expect(op.operation.variables['initialCash']).toBe(50000);
    expect(op.operation.variables['fillMode']).toBe('signal_bar_close');

    const specJson = op.operation.variables['specJson'] as string;
    expect(typeof specJson).toBe('string');
    const parsed = JSON.parse(specJson);
    expect(parsed.name).toBe('spec-service-test');
    expect(parsed.symbols).toEqual(['SPY']);

    op.flush({
      data: {
        runSpecStrategyBacktest: {
          success: true,
          strategyName: 'spec-service-test',
          initialCash: 50000,
          finalEquity: 51200,
          netProfit: 1200,
          totalFees: 0,
          totalTrades: 1,
          winningTrades: 1,
          losingTrades: 0,
          winRate: 1.0,
          // entryTime / exitTime are int64 ms UTC (per the wire-format
          // rule), not ISO strings. 1704153600000 = 2024-01-02 00:00 UTC.
          trades: [
            {
              tradeNumber: 1,
              entryTime: 1704153600000,
              entryPrice: 470.5,
              exitTime: 1704157200000,
              exitPrice: 472.1,
              // Indicators arrive as a list-of-DTO from GraphQL — Hot
              // Chocolate v15's Dictionary<string, decimal> exposure
              // would need awkward sub-field selection, so the backend
              // projects to IndicatorSnapshotEntry[] at the boundary.
              indicators: [
                { name: 'sma_s', value: 470.4 },
                { name: 'sma_l', value: 470.0 },
              ],
              pnlPts: 1.6,
              pnlPct: 0.0034,
              result: 'WIN',
              signalReason: 'test',
            },
          ],
          logLines: ['ok'],
          error: null,
        },
      },
    });

    const result = await promise;
    expect(result.success).toBe(true);
    expect(result.totalTrades).toBe(1);
    expect(result.winRate).toBe(1.0);
    // Wire-format check — TS type declares entryTime/exitTime as number,
    // and Apollo passes the JSON int through unchanged.
    const trade = result.trades[0];
    expect(typeof trade.entryTime).toBe('number');
    expect(trade.entryTime).toBe(1704153600000);
    expect(trade.exitTime).toBe(1704157200000);
    // Indicators arrive as a list of {name, value} entries.
    expect(Array.isArray(trade.indicators)).toBe(true);
    expect(trade.indicators).toHaveLength(2);
    expect(trade.indicators[0]).toEqual({ name: 'sma_s', value: 470.4 });
  });

  it('exposes loading and result via signals', async () => {
    expect(service.loading()).toBe(false);
    expect(service.result()).toBeNull();

    const promise = service.runBacktest(TRIVIAL_SPEC, {
      startDate: '2024-01-02',
      endDate: '2024-12-31',
    });

    expect(service.loading()).toBe(true);

    const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
    op.flush({
      data: {
        runSpecStrategyBacktest: {
          success: true,
          strategyName: TRIVIAL_SPEC.name,
          initialCash: 100000,
          finalEquity: 100000,
          netProfit: 0,
          totalFees: 0,
          totalTrades: 0,
          winningTrades: 0,
          losingTrades: 0,
          winRate: 0,
          trades: [],
          logLines: [],
          error: null,
        },
      },
    });

    await promise;
    expect(service.loading()).toBe(false);
    expect(service.result()?.success).toBe(true);
  });

  it('surfaces error when GraphQL returns success=false', async () => {
    const promise = service.runBacktest(TRIVIAL_SPEC, {
      startDate: '2024-01-02',
      endDate: '2024-12-31',
    });

    const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
    op.flush({
      data: {
        runSpecStrategyBacktest: {
          success: false,
          strategyName: '',
          initialCash: 0,
          finalEquity: 0,
          netProfit: 0,
          totalFees: 0,
          totalTrades: 0,
          winningTrades: 0,
          losingTrades: 0,
          winRate: 0,
          trades: [],
          logLines: [],
          error: 'spec uses unsupported feature: option template',
        },
      },
    });

    const result = await promise;
    expect(result.success).toBe(false);
    expect(result.error).toContain('option template');
    expect(service.error()).toContain('option template');
  });
});
