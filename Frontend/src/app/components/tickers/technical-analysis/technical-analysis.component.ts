import { Component, inject, signal, computed, resource, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from '../../../services/market-data.service';
import { StockAggregate, IndicatorSeries } from '../../../graphql/types';
import { TaChartComponent } from '../ta-chart/ta-chart.component';

interface AnalysisRequest {
  ticker: string;
  fromDate: string;
  toDate: string;
  timespan: string;
  multiplier: number;
  indicators: { name: string; window: number }[];
  trigger: number;
}

interface AnalysisResult {
  aggregates: StockAggregate[];
  indicators: IndicatorSeries[];
  message: string | null;
}

@Component({
  selector: 'app-technical-analysis',
  standalone: true,
  imports: [CommonModule, FormsModule, TaChartComponent],
  templateUrl: './technical-analysis.component.html',
  styleUrls: ['./technical-analysis.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TechnicalAnalysisComponent {
  private marketDataService = inject(MarketDataService);

  ticker = signal('AAPL');
  fromDate = signal(defaultFromDate());
  toDate = signal(defaultToDate());
  timespan = signal('day');
  multiplier = signal(1);

  showSma = signal(true);
  smaWindow = signal(20);
  showEma = signal(true);
  emaWindow = signal(50);
  showRsi = signal(true);
  rsiWindow = signal(14);

  private analyzeRequest = signal<AnalysisRequest | undefined>(undefined);

  analysisResource = resource<AnalysisResult | undefined, AnalysisRequest | undefined>({
    params: () => this.analyzeRequest(),
    loader: async ({ params }) => {
      if (!params) return undefined;

      const aggResult = await firstValueFrom(
        this.marketDataService.getOrFetchStockAggregates(
          params.ticker, params.fromDate, params.toDate,
          params.timespan, params.multiplier
        )
      );

      const aggregates = [...aggResult.aggregates].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      );

      if (params.indicators.length === 0) {
        return { aggregates, indicators: [], message: 'No indicators selected. Enable at least one above.' };
      }

      const indResult = await firstValueFrom(
        this.marketDataService.calculateIndicators(
          params.ticker, params.fromDate, params.toDate,
          params.indicators, params.timespan, params.multiplier
        )
      );

      return { aggregates, indicators: indResult.indicators, message: indResult.message };
    }
  });

  aggregates = computed(() => this.analysisResource.value()?.aggregates ?? []);
  indicators = computed(() => this.analysisResource.value()?.indicators ?? []);
  message = computed(() => this.analysisResource.value()?.message ?? null);
  loading = this.analysisResource.isLoading;
  error = computed(() => {
    const err = this.analysisResource.error();
    return err ? (err as Error).message ?? String(err) : null;
  });

  private triggerCount = 0;

  fetchAndCalculate(): void {
    const t = this.ticker();
    if (!t) return;

    const indicatorConfigs: { name: string; window: number }[] = [];
    if (this.showSma()) indicatorConfigs.push({ name: 'sma', window: this.smaWindow() });
    if (this.showEma()) indicatorConfigs.push({ name: 'ema', window: this.emaWindow() });
    if (this.showRsi()) indicatorConfigs.push({ name: 'rsi', window: this.rsiWindow() });

    this.analyzeRequest.set({
      ticker: t.toUpperCase(),
      fromDate: this.fromDate(),
      toDate: this.toDate(),
      timespan: this.timespan(),
      multiplier: this.multiplier(),
      indicators: indicatorConfigs,
      trigger: ++this.triggerCount,
    });
  }
}

function defaultFromDate(): string {
  const d = new Date();
  d.setMonth(d.getMonth() - 6);
  return d.toISOString().split('T')[0];
}

function defaultToDate(): string {
  return new Date().toISOString().split('T')[0];
}
