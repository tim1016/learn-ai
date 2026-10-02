import { signal, type Provider } from '@angular/core';
import { of } from 'rxjs';
import type { Mock, VitestUtils } from 'vitest';

import { BotChartIndicatorService } from '../components/broker/v2-panel/dual-pane-chart/bot-chart-indicator.service';
import type { GateEvaluationResponse, StrategyGateList } from '../components/broker/v2-panel/lib/broker-v2-panel.types';
import { StrategyGatesService } from '../components/broker/v2-panel/strategy-view/strategy-gates.service';
import {
  IndicatorCatalogService,
  type IndicatorCategory,
} from '../shared/indicator-catalog/indicator-catalog.service';

/**
 * Stand-ins for what the bot chart panel reads from the data plane beside the
 * strategy view (#2639): the strategy's custom gates and their judgement, and
 * the chart's indicator catalogue. `HttpClient` needs no provider, so a spec
 * that renders the panel without these sends real requests that fail.
 *
 * Takes the spec's own `vi`: a helper importing vitest at runtime is
 * evaluated before the spec's hoisted `vi.mock` calls.
 */
export interface StrategyViewDataPlaneFakes {
  readonly providers: Provider[];
  readonly gates: {
    readonly list: Mock<(strategyKey: string) => Promise<StrategyGateList>>;
    readonly create: Mock;
    readonly replace: Mock;
    readonly remove: Mock;
    readonly evaluate: Mock;
  };
  readonly indicators: {
    readonly supportedIndicators: Mock;
    readonly calculate: Mock;
    readonly calculateBars: Mock;
  };
  readonly catalog: {
    readonly load: Mock;
    readonly categories: ReturnType<typeof signal<IndicatorCategory[]>>;
    readonly loading: () => boolean;
    readonly failed: () => boolean;
  };
}

export const NO_GATE_RESULTS: GateEvaluationResponse = { results: {}, chart_computed: [], notices: [] };

export function fakeStrategyViewDataPlane(
  vi: VitestUtils,
  catalogue: IndicatorCategory[] = [],
): StrategyViewDataPlaneFakes {
  const gates = {
    list: vi.fn((strategyKey: string) => Promise.resolve<StrategyGateList>({ strategy_key: strategyKey, gates: [] })),
    create: vi.fn(),
    replace: vi.fn(),
    remove: vi.fn(() => Promise.resolve()),
    evaluate: vi.fn(() => Promise.resolve(NO_GATE_RESULTS)),
  };
  const indicators = {
    supportedIndicators: vi.fn(() => of({
      names: catalogue.flatMap((category) => category.indicators.map((indicator) => indicator.name)),
    })),
    calculate: vi.fn(() => of({ symbol: 'SPY', indicators: [] })),
    calculateBars: vi.fn(() => of({ symbol: 'SPY', indicators: [] })),
  };
  const catalog = {
    load: vi.fn(() => Promise.resolve()),
    categories: signal<IndicatorCategory[]>(catalogue),
    loading: () => false,
    failed: () => false,
  };
  return {
    gates,
    indicators,
    catalog,
    providers: [
      { provide: StrategyGatesService, useValue: gates },
      { provide: BotChartIndicatorService, useValue: indicators },
      { provide: IndicatorCatalogService, useValue: catalog },
    ],
  };
}
