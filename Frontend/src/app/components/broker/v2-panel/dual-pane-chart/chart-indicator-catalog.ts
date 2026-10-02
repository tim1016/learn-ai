import { computed, inject, type Signal } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';

import {
  IndicatorCatalogService,
  type IndicatorCategory,
} from '../../../../shared/indicator-catalog/indicator-catalog.service';
import { BotChartIndicatorService } from './bot-chart-indicator.service';

/** The indicators a bot chart can draw: the shared catalogue, narrowed to the ones the chart computes. */
export interface ChartIndicatorCatalog {
  readonly categories: Signal<readonly IndicatorCategory[]>;
  readonly loading: Signal<boolean>;
  /** The shared catalogue (`/api/dataset/available`) failed. */
  readonly loadFailed: Signal<boolean>;
  /** The chart's supported set failed, so nothing can be offered. */
  readonly supportFailed: Signal<boolean>;
}

/**
 * The bot charts' indicator catalogue, for the market tape and the strategy
 * view alike. Call it in an injection context.
 *
 * Every read is guarded (#2202): the supported set's `value()` throws while
 * its fetch is in its error state, which would abort the host's render pass.
 */
export function chartIndicatorCatalog(): ChartIndicatorCatalog {
  const catalog = inject(IndicatorCatalogService);
  const indicators = inject(BotChartIndicatorService);
  const supported = rxResource({
    params: () => 'chart-indicator-catalog',
    stream: () => indicators.supportedIndicators(),
  });
  void catalog.load();
  return {
    categories: computed(() => {
      const names = new Set(supported.hasValue() ? supported.value()?.names ?? [] : []);
      return catalog.categories()
        .map((category) => ({
          ...category,
          indicators: category.indicators.filter((indicator) => names.has(indicator.name)),
        }))
        .filter((category) => category.indicators.length > 0);
    }),
    loading: computed(() => catalog.loading() || supported.isLoading()),
    loadFailed: catalog.failed,
    supportFailed: computed(() => supported.error() !== undefined),
  };
}
