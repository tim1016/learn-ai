import { TestBed } from '@angular/core/testing';
import { ApolloTestingController, ApolloTestingModule } from 'apollo-angular/testing';
import { provideZonelessChangeDetection } from '@angular/core';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { RUN_SPEC_STRATEGY_BACKTEST } from '../../services/spec-strategy.service';
import { SpecStrategyRunnerComponent } from './spec-strategy-runner.component';
import { CANONICAL_FIXTURES } from './canonical-fixtures';
import { insertSnippet, SpecSnippet } from './snippets';

/**
 * Sanity tests for the runner component. We don't render with
 * @testing-library here because the component is template-heavy and
 * the cheap things we want to prove — fixture bundling, picker
 * selection, JSON dirty-tracking, mutation wiring — are all
 * accessible via the component's signal surface and a stub Apollo
 * controller.
 *
 * Browser-level UI verification (form behavior, table rendering,
 * accessibility) is left for the maintainer running the dev server.
 */
describe('SpecStrategyRunnerComponent', () => {
  let component: SpecStrategyRunnerComponent;
  let controller: ApolloTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [SpecStrategyRunnerComponent, ApolloTestingModule],
      providers: [provideZonelessChangeDetection()],
    });
    component = TestBed.createComponent(SpecStrategyRunnerComponent).componentInstance;
    controller = TestBed.inject(ApolloTestingController);
  });

  afterEach(() => {
    controller.verify();
  });

  it('exposes the three canonical fixtures and starts on SPY EMA', () => {
    expect(component.fixtures).toHaveLength(3);
    expect(component.fixtures.map((f) => f.id)).toEqual([
      'spy_ema_crossover',
      'sma_crossover',
      'rsi_mean_reversion',
    ]);
    expect(component.selectedFixtureId()).toBe('spy_ema_crossover');
  });

  it('selectFixture rewrites the JSON textarea and resets edited marker', () => {
    // Pretend the user has edited the JSON.
    component.specJson.set('{"i": "edited"}');
    expect(component.isPristine()).toBe(false);

    component.selectFixture('rsi_mean_reversion');

    expect(component.selectedFixtureId()).toBe('rsi_mean_reversion');
    expect(component.isPristine()).toBe(true);
    const fixture = CANONICAL_FIXTURES.find((f) => f.id === 'rsi_mean_reversion');
    expect(fixture).toBeDefined();
    const expected = JSON.stringify(fixture?.spec, null, 2);
    expect(component.specJson()).toBe(expected);
  });

  it('runBacktest fires the mutation with the JSON payload', async () => {
    const promise = component.runBacktest();

    const op = controller.expectOne(RUN_SPEC_STRATEGY_BACKTEST);
    expect(op.operation.variables['startDate']).toBe('2024-03-28');
    expect(op.operation.variables['endDate']).toBe('2024-12-31');
    expect(op.operation.variables['initialCash']).toBe(100000);
    expect(op.operation.variables['fillMode']).toBe('signal_bar_close');

    op.flush({
      data: {
        runSpecStrategyBacktest: {
          success: true,
          strategyName: 'spy ema test',
          initialCash: 100000,
          finalEquity: 101000,
          netProfit: 1000,
          totalFees: 0,
          totalTrades: 1,
          winningTrades: 1,
          losingTrades: 0,
          winRate: 1.0,
          trades: [
            {
              tradeNumber: 1,
              entryTime: 1704153600000,
              entryPrice: 470,
              exitTime: 1704157200000,
              exitPrice: 471,
              indicators: [{ name: 'ema5', value: 470.4 }],
              pnlPts: 1,
              pnlPct: 0.0021,
              result: 'WIN',
              signalReason: 'test',
            },
          ],
          logLines: [],
          error: null,
        },
      },
    });

    await promise;
    expect(component.result()?.success).toBe(true);
    expect(component.tradeCount()).toBe(1);
  });

  it('runBacktest surfaces JSON parse errors as localError without firing the mutation', async () => {
    component.specJson.set('this is not valid json {');

    await component.runBacktest();

    expect(component.localError()).toContain('Invalid JSON');
    // No mutation should have been issued. controller.verify() in afterEach
    // would fail if there were unconsumed expectations, so we just assert
    // that no apollo operation was triggered by inspecting the result.
    expect(component.result()).toBeNull();
  });

  it('formatTime renders an ms-UTC timestamp in America/New_York regardless of browser tz', () => {
    // 1704153600000 ms = 2024-01-02 00:00:00 UTC = 2024-01-01 19:00 ET (EST).
    // Locked to ET so screenshots / shared analyses are unambiguous
    // regardless of where the viewer is.
    const out = component.formatTime(1704153600000);
    expect(out).toBe('01/01/2024, 19:00');
  });

  // ---- Snippet catalog -------------------------------------------------
  it('exposes the snippet catalog with indicator / condition / survival groups', () => {
    const titles = component.snippetGroups.map((g) => g.title);
    expect(titles).toContain('Indicators');
    expect(titles).toContain('Conditions');
    expect(titles).toContain('Survival rules (Manage)');
    // All six engine indicator kinds must be discoverable.
    const indicatorGroup = component.snippetGroups.find((g) => g.title === 'Indicators');
    expect(indicatorGroup).toBeDefined();
    const indicatorLabels = (indicatorGroup?.snippets ?? []).map((s) => s.label);
    for (const kind of ['SMA', 'EMA', 'RSI', 'ADX', 'MACD', 'SUPERTREND']) {
      expect(indicatorLabels.some((l) => l.startsWith(kind))).toBe(true);
    }
  });

  it('insertSnippetIntoSpec appends an indicator to the indicators array', () => {
    component.specJson.set(
      JSON.stringify(
        {
          schema_version: '1.0',
          name: 'tmp',
          symbols: ['SPY'],
          resolution: { period_minutes: 15 },
          indicators: [{ id: 'sma', kind: 'SMA', period: 10 }],
          entry: { logic: 'AND', conditions: [], size: { kind: 'SetHoldings', fraction: 1 } },
          exit: { logic: 'OR', conditions: [] },
        },
        null,
        2,
      ),
    );
    const adxSnippet: SpecSnippet = {
      id: 'ind.adx',
      label: 'ADX',
      description: '',
      target: 'indicators',
      example: { id: 'adx_14', kind: 'ADX', period: 14 },
    };
    component.insertSnippetIntoSpec(adxSnippet);

    const after = JSON.parse(component.specJson()) as { indicators: object[] };
    expect(after.indicators).toHaveLength(2);
    expect(after.indicators[1]).toEqual({ id: 'adx_14', kind: 'ADX', period: 14 });
    expect(component.catalogStatus()).toContain('Inserted');
    expect(component.localError()).toBeNull();
  });

  it('insertSnippetIntoSpec surfaces a parse error when spec is invalid JSON', () => {
    component.specJson.set('not valid json {');
    const snippet: SpecSnippet = {
      id: 'x',
      label: 'X',
      description: '',
      target: 'indicators',
      example: { id: 'x', kind: 'SMA', period: 5 },
    };
    component.insertSnippetIntoSpec(snippet);

    expect(component.localError()).toContain('spec JSON is invalid');
    expect(component.catalogStatus()).toBeNull();
  });

  it('insertSnippet (pure helper) routes by target into the right array', () => {
    const base = JSON.stringify(
      {
        schema_version: '1.0',
        name: 'x',
        symbols: ['SPY'],
        resolution: { period_minutes: 15 },
        indicators: [],
        entry: { logic: 'AND', conditions: [], size: { kind: 'SetHoldings', fraction: 1 } },
        survival: [],
        exit: { logic: 'OR', conditions: [] },
      },
      null,
      2,
    );

    const survivalRule = {
      id: 'surv',
      label: 'stop',
      description: '',
      target: 'survival' as const,
      example: { name: 'stop', when: { logic: 'AND', conditions: [] }, action: { kind: 'CLOSE_ALL' } },
    };
    const out = JSON.parse(insertSnippet(base, survivalRule)) as { survival: object[] };
    expect(out.survival).toHaveLength(1);
    expect(out.survival[0]).toMatchObject({ name: 'stop' });
  });

  it('formatIndicators serializes a list-of-DTO trade to "name=value" pairs', () => {
    const trade = {
      tradeNumber: 1,
      entryTime: 0,
      entryPrice: 0,
      exitTime: 0,
      exitPrice: 0,
      indicators: [
        { name: 'ema5', value: 470.4321 },
        { name: 'rsi', value: 62.5 },
      ],
      pnlPts: 0,
      pnlPct: 0,
      result: 'WIN' as const,
      signalReason: '',
    };
    expect(component.formatIndicators(trade)).toBe('ema5=470.4321, rsi=62.5000');
  });
});
