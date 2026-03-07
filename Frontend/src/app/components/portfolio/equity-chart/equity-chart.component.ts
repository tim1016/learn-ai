import {
  Component, ChangeDetectionStrategy, input, signal, inject,
  DestroyRef, effect, ViewChild, ElementRef, AfterViewInit, OnDestroy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of, forkJoin } from 'rxjs';
import { createChart, IChartApi, ISeriesApi, LineSeries, AreaSeries, UTCTimestamp, HistogramSeries } from 'lightweight-charts';
import { PortfolioService } from '../../../services/portfolio.service';
import { PortfolioSnapshot, DrawdownPoint, PortfolioMetrics } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-equity-chart',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './equity-chart.component.html',
  styleUrls: ['./equity-chart.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class EquityChartComponent implements AfterViewInit, OnDestroy {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  @ViewChild('equityChartContainer') equityChartContainer!: ElementRef<HTMLDivElement>;
  @ViewChild('drawdownChartContainer') drawdownChartContainer!: ElementRef<HTMLDivElement>;

  snapshots = signal<PortfolioSnapshot[]>([]);
  drawdownSeries = signal<DrawdownPoint[]>([]);
  metrics = signal<PortfolioMetrics | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  private equityChart: IChartApi | null = null;
  private drawdownChart: IChartApi | null = null;
  private resizeObservers: ResizeObserver[] = [];

  constructor() {
    effect(() => { if (this.accountId()) this.load(); });
  }

  ngAfterViewInit(): void {
    this.createCharts();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    const id = this.accountId();

    forkJoin({
      equity: this.portfolioService.getEquityCurve(id),
      drawdown: this.portfolioService.getDrawdownSeries(id),
      metrics: this.portfolioService.getMetrics(id).pipe(catchError(() => of(null))),
    }).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(result => {
      if (result) {
        this.snapshots.set(result.equity ?? []);
        this.drawdownSeries.set(result.drawdown ?? []);
        this.metrics.set(result.metrics ?? null);
        this.updateCharts();
      }
    });
  }

  private createCharts(): void {
    if (this.equityChartContainer) {
      const container = this.equityChartContainer.nativeElement;
      this.equityChart = createChart(container, {
        width: container.clientWidth,
        height: 300,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
        rightPriceScale: { borderColor: '#ddd' },
        timeScale: { borderColor: '#ddd' },
      });
      this.observeResize(container, this.equityChart);
    }

    if (this.drawdownChartContainer) {
      const container = this.drawdownChartContainer.nativeElement;
      this.drawdownChart = createChart(container, {
        width: container.clientWidth,
        height: 200,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
        rightPriceScale: { borderColor: '#ddd' },
        timeScale: { borderColor: '#ddd' },
      });
      this.observeResize(container, this.drawdownChart);
    }
  }

  private updateCharts(): void {
    if (!this.equityChartContainer || !this.drawdownChartContainer) return;

    if (!this.equityChart || !this.drawdownChart) {
      this.createCharts();
    }

    // Equity curve
    if (this.equityChart && this.snapshots().length) {
      // Remove all existing series by recreating
      this.equityChart.remove();
      const container = this.equityChartContainer.nativeElement;
      this.equityChart = createChart(container, {
        width: container.clientWidth,
        height: 300,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
        rightPriceScale: { borderColor: '#ddd' },
        timeScale: { borderColor: '#ddd' },
      });

      const equitySeries = this.equityChart.addSeries(AreaSeries, {
        lineColor: '#2962FF',
        topColor: 'rgba(41, 98, 255, 0.3)',
        bottomColor: 'rgba(41, 98, 255, 0.02)',
        lineWidth: 2,
      });

      const data = this.snapshots()
        .map(s => ({
          time: (new Date(s.timestamp).getTime() / 1000) as UTCTimestamp,
          value: s.equity,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));
      equitySeries.setData(data);
      this.equityChart.timeScale().fitContent();
    }

    // Drawdown chart
    if (this.drawdownChart && this.drawdownSeries().length) {
      this.drawdownChart.remove();
      const container = this.drawdownChartContainer.nativeElement;
      this.drawdownChart = createChart(container, {
        width: container.clientWidth,
        height: 200,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
        rightPriceScale: { borderColor: '#ddd' },
        timeScale: { borderColor: '#ddd' },
      });

      const ddSeries = this.drawdownChart.addSeries(HistogramSeries, {
        color: '#ef5350',
      });

      const ddData = this.drawdownSeries()
        .map(d => ({
          time: (new Date(d.timestamp).getTime() / 1000) as UTCTimestamp,
          value: -d.drawdownPercent * 100,
          color: d.drawdownPercent > 0.05 ? '#ef5350' : '#ff9800',
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));
      ddSeries.setData(ddData);
      this.drawdownChart.timeScale().fitContent();
    }
  }

  private observeResize(container: HTMLDivElement, chart: IChartApi): void {
    const observer = new ResizeObserver(entries => {
      if (entries.length > 0) {
        chart.applyOptions({ width: entries[0].contentRect.width });
      }
    });
    observer.observe(container);
    this.resizeObservers.push(observer);
  }

  ngOnDestroy(): void {
    this.resizeObservers.forEach(o => o.disconnect());
    this.equityChart?.remove();
    this.drawdownChart?.remove();
  }
}
