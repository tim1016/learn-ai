import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';

import { etIsoDate } from '../../../../shared/date/et-midnight';
import { CandlestickChartComponent } from '../../../../shared/charts/candlestick-chart/candlestick-chart.component';
import { ReturnsDistributionService } from '../returns-distribution.service';

/**
 * The "inspect the day" pane: extended-session minute candles for one
 * selected trading date, fetched lazily from the study's own day-candles
 * read (same lake root as the study, raw prices: one day's adjustment is
 * one constant, so the candles' shape matches the return being
 * inspected). The candles render through
 * the shared canonical chart; this wrapper owns only the fetch and the ET
 * date heading.
 */
@Component({
  selector: 'app-day-candles',
  template: `
    <section class="candles" aria-label="Minute candles for the selected day">
      <h3 class="candles__title">
        {{ etDate() }} — full trading day (incl. pre/post market)
      </h3>
      @if (candles.isLoading()) {
        <p class="candles__status">Loading minute bars…</p>
      } @else if (candles.error()) {
        <p class="candles__status candles__status--error" role="alert">
          Minute bars unavailable for this day.
        </p>
      } @else if (hasBars()) {
        <app-candlestick-chart [data]="bars()" [ticker]="ticker()" [timeVisible]="true" />
      } @else {
        <p class="candles__status">No minute bars recorded for this day.</p>
      }
    </section>
  `,
  styles: `
    :host { display: block; }
    .candles__title { margin: 0 0 0.5rem; font-size: 1rem; font-weight: 600; }
    .candles__status { color: var(--text-color-secondary, #94a3b8); margin: 0; }
    .candles__status--error { color: #ef4444; }
  `,
  imports: [CandlestickChartComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DayCandlesComponent {
  readonly ticker = input.required<string>();
  readonly sessionOpenMsUtc = input.required<number>();

  private readonly service = inject(ReturnsDistributionService);

  readonly etDate = computed(() => etIsoDate(this.sessionOpenMsUtc()));

  readonly candles = rxResource({
    params: () => ({ ticker: this.ticker(), ms: this.sessionOpenMsUtc() }),
    stream: ({ params }) => this.service.minuteCandles(params.ticker, params.ms),
  });

  readonly bars = computed(() => this.candles.value() ?? []);
  readonly hasBars = computed(() => this.bars().length > 0);
}
