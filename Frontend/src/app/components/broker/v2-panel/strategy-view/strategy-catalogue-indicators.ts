import { computed, inject, linkedSignal, type Signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { catchError, map, of } from 'rxjs';

import type { IndicatorPickerAdd } from '../../../../shared/indicator-picker/indicator-picker.component';
import type { ChartIndicatorResult, TradingIndicatorChip } from '../../../../shared/trading-chart';
import { BotChartIndicatorService } from '../dual-pane-chart/bot-chart-indicator.service';
import { chartIndicatorCatalog, type ChartIndicatorCatalog } from '../dual-pane-chart/chart-indicator-catalog';
import {
  indicatorSeriesPlans,
  resultBelongsToIndicator,
  selectChartIndicator,
  toActiveIndicatorChips,
  type IndicatorSeriesPlan,
  type SelectedChartIndicator,
} from '../dual-pane-chart/dual-pane-chart-indicators';
import type { StrategyViewResponse } from '../lib/broker-v2-panel.types';
import { settledRead } from './settled-read';
import { strategyChartTimes, strategyIndicatorBars } from './strategy-view-model';

interface CalculatedIndicators {
  /** The strategy whose candles these were computed on: a kept answer never crosses to another. */
  readonly strategyKey: string;
  readonly results: readonly ChartIndicatorResult[];
  readonly error: string | null;
}

/** The catalogue indicators a viewer put on a strategy view, computed on its lead-in bars and decision candles. */
export interface StrategyCatalogueIndicators {
  readonly catalog: ChartIndicatorCatalog;
  readonly chips: Signal<readonly TradingIndicatorChip[]>;
  readonly keys: Signal<readonly string[]>;
  readonly plans: Signal<readonly IndicatorSeriesPlan[]>;
  readonly calculating: Signal<boolean>;
  readonly error: Signal<string | null>;
  add(entry: IndicatorPickerAdd): void;
  remove(id: string): void;
}

/**
 * Catalogue indicators on a strategy view (#2639 D12). Call it in an
 * injection context, with the view the host read.
 *
 * These lines are the chart's own computation, never the bot's: the
 * strategy's recorded values stay the only lines drawn from what the bot saw.
 * They are computed over the view's lead-in bars and then its decision
 * candles, so a line has warmed up by the first candle wherever the view
 * carries enough lead-in (#2800). The choice lasts for the visit and resets
 * with the strategy. While a newer read is computed the last lines stay
 * drawn; points are placed by bar close, so they never land on the wrong
 * candle and none is drawn for a lead-in bar.
 */
export function strategyCatalogueIndicators(view: Signal<StrategyViewResponse | null>): StrategyCatalogueIndicators {
  const indicators = inject(BotChartIndicatorService);
  const catalog = chartIndicatorCatalog();
  // A re-read of the same strategy reruns this computation too; only a new
  // strategy starts the choice over.
  const selected = linkedSignal<string | null, readonly SelectedChartIndicator[]>({
    source: () => view()?.strategy_key ?? null,
    computation: (strategyKey, previous) => (previous?.source === strategyKey ? previous.value : []),
  });

  const calculation = rxResource<CalculatedIndicators, {
    symbol: string;
    view: StrategyViewResponse;
    selected: readonly SelectedChartIndicator[];
  } | undefined>({
    params: () => {
      const read = view();
      const chosen = selected();
      return read === null || chosen.length === 0 || read.candles.length === 0
        ? undefined
        : { symbol: read.symbol, view: read, selected: chosen };
    },
    stream: ({ params }) => indicators.calculateBars(params.symbol, strategyIndicatorBars(params.view), params.selected).pipe(
      map((response): CalculatedIndicators => ({
        strategyKey: params.view.strategy_key,
        results: response.indicators,
        error: null,
      })),
      catchError(() => of<CalculatedIndicators>({
        strategyKey: params.view.strategy_key,
        results: [],
        error: 'Indicators could not be calculated on these candles.',
      })),
    ),
  });
  // The last settled answer, kept while a newer read is computed.
  const settled = settledRead(calculation);
  /** The kept answer, while it is for the strategy shown. */
  const current = computed(() => {
    const calculated = settled().value;
    return calculated !== null && calculated.strategyKey === view()?.strategy_key ? calculated : null;
  });
  // Only what is still selected: a removed indicator's line goes at once.
  const results = computed(() => {
    const chosen = selected();
    return (current()?.results ?? []).filter((result) => chosen.some((entry) => resultBelongsToIndicator(result, entry)));
  });

  return {
    catalog,
    chips: computed(() => toActiveIndicatorChips(selected(), results())),
    keys: computed(() => selected().map((indicator) => indicator.name)),
    plans: computed(() => {
      const read = view();
      return read === null ? [] : indicatorSeriesPlans(results(), strategyChartTimes(read.candles), selected());
    }),
    calculating: computed(() => selected().length > 0 && calculation.isLoading()),
    error: computed(() => {
      if (catalog.supportFailed()) return 'The chart indicator catalogue could not be loaded.';
      return current()?.error ?? null;
    }),
    add: (entry) => selected.update((current) => selectChartIndicator(current, entry)),
    remove: (id) => selected.update((current) => current.filter((indicator) => indicator.id !== id)),
  };
}
