import {
  Component,
  signal,
  computed,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { InputNumber } from 'primeng/inputnumber';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ProgressSpinner } from 'primeng/progressspinner';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { TooltipModule } from 'primeng/tooltip';
import { Checkbox } from 'primeng/checkbox';
import { MultiSelect } from 'primeng/multiselect';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

// ─── Interfaces ────────────────────────────────────────────

interface HorizonICResult {
  horizon: number;
  mean_ic: number;
  t_stat: number;
  p_value: number;
  nw_t_stat: number | null;
  nw_p_value: number | null;
  effective_n: number;
  interpretation: string;
}

interface IndicatorReliabilityResponse {
  success: boolean;
  ticker: string;
  indicator_name: string;
  indicator_params: Record<string, number>;
  display_name: string;
  category: string | null;
  start_date: string;
  end_date: string;
  bar_count: number;
  results: HorizonICResult[];
  slope_results: HorizonICResult[] | null;
  daily_ic_values: number[];
  daily_ic_dates: string[];
  best_horizon: number | null;
  error: string | null;
}

interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  params: ParamConfig[];
}

interface ParamConfig {
  name: string;
  type: string;
  default: number;
  min?: number;
  max?: number;
  description: string;
}

interface IndicatorOption {
  label: string;
  value: string;
  category: string;
  description: string;
}

interface HorizonOption {
  label: string;
  value: number;
}

@Component({
  selector: 'app-indicator-reliability',
  templateUrl: './indicator-reliability.component.html',
  styleUrls: ['./indicator-reliability.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    FormsModule,
    Select,
    InputText,
    InputNumber,
    ButtonModule,
    MessageModule,
    ProgressSpinner,
    TableModule,
    TagModule,
    TooltipModule,
    Checkbox,
    MultiSelect,
  ],
})
export class IndicatorReliabilityComponent {
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private pythonUrl = environment.pythonServiceUrl;

  icChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('icChart');
  private icChart: Chart | null = null;

  // Form inputs
  ticker = signal('AAPL');
  indicatorName = signal('rsi');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-06-30');
  includeSlope = signal(false);

  // Dynamic params (populated when indicator is selected)
  paramConfigs = signal<ParamConfig[]>([]);
  paramValues = signal<Record<string, number>>({});

  // Horizons multi-select
  allHorizons: HorizonOption[] = [
    { label: '1-bar', value: 1 },
    { label: '5-bar', value: 5 },
    { label: '10-bar', value: 10 },
    { label: '15-bar', value: 15 },
    { label: '30-bar', value: 30 },
  ];
  selectedHorizons = signal<number[]>([1, 5, 10, 15, 30]);

  // State
  loading = signal(false);
  loadingIndicators = signal(false);
  result = signal<IndicatorReliabilityResponse | null>(null);
  error = signal<string | null>(null);
  formCollapsed = signal(false);

  // Indicator options (loaded from API)
  indicatorOptions = signal<IndicatorOption[]>([]);
  groupedIndicators = signal<Record<string, IndicatorOption[]>>({});

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.indicatorName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
      this.selectedHorizons().length > 0 &&
      !this.loading()
    );
  });

  get selectedIndicatorLabel(): string {
    const found = this.indicatorOptions().find(i => i.value === this.indicatorName());
    return found ? found.label : this.indicatorName().toUpperCase();
  }

  constructor() {
    // Load indicators on init
    this.loadIndicators();

    // Load params when indicator changes
    effect(() => {
      const name = this.indicatorName();
      if (name) {
        this.loadIndicatorParams(name);
      }
    });

    // Render chart when result changes
    effect(() => {
      const res = this.result();
      const canvas = this.icChartCanvas();
      if (res && canvas && res.daily_ic_values.length > 0) {
        this.renderIcChart(canvas.nativeElement, res);
      }
    });
  }

  private loadIndicators(): void {
    this.loadingIndicators.set(true);
    this.http
      .get<Record<string, IndicatorInfo[]>>(`${this.pythonUrl}/api/research/indicators`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          console.error('Failed to load indicators:', err);
          return of({});
        }),
        finalize(() => this.loadingIndicators.set(false)),
      )
      .subscribe(categories => {
        const options: IndicatorOption[] = [];
        const grouped: Record<string, IndicatorOption[]> = {};

        for (const [category, indicators] of Object.entries(categories)) {
          grouped[category] = [];
          for (const ind of indicators) {
            const opt: IndicatorOption = {
              label: ind.name.toUpperCase(),
              value: ind.name,
              category,
              description: ind.description,
            };
            options.push(opt);
            grouped[category].push(opt);
          }
        }

        this.indicatorOptions.set(options);
        this.groupedIndicators.set(grouped);
      });
  }

  private loadIndicatorParams(name: string): void {
    this.http
      .get<ParamConfig[]>(`${this.pythonUrl}/api/research/indicator-params/${name}`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(() => of([])),
      )
      .subscribe(params => {
        this.paramConfigs.set(params);
        // Set default values
        const defaults: Record<string, number> = {};
        for (const p of params) {
          defaults[p.name] = p.default;
        }
        this.paramValues.set(defaults);
      });
  }

  updateParam(name: string, value: number): void {
    this.paramValues.update(current => ({ ...current, [name]: value }));
  }

  runAnalysis(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    const payload = {
      ticker: this.ticker().toUpperCase(),
      indicator_name: this.indicatorName(),
      indicator_params: this.paramValues(),
      start_date: this.fromDate(),
      end_date: this.toDate(),
      horizons: this.selectedHorizons(),
      include_slope: this.includeSlope(),
      timespan: 'minute',
      multiplier: 1,
    };

    this.http
      .post<IndicatorReliabilityResponse>(
        `${this.pythonUrl}/api/research/indicator-reliability`,
        payload,
      )
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          const msg = err?.error?.detail ?? err?.message ?? 'An unexpected error occurred';
          this.error.set(msg);
          return of(null);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(res => {
        if (res) {
          this.result.set(res);
          if (!res.success && res.error) {
            this.error.set(res.error);
          } else if (res.success) {
            this.formCollapsed.set(true);
          }
        }
      });
  }

  toggleForm(): void {
    this.formCollapsed.update(v => !v);
  }

  newRun(): void {
    this.formCollapsed.set(false);
    this.result.set(null);
    this.error.set(null);
  }

  // ─── Helpers for Display ─────────────────────────────────

  getIcSeverity(meanIc: number, pValue: number): 'success' | 'warn' | 'danger' | 'info' {
    const absIc = Math.abs(meanIc);
    if (pValue >= 0.10) return 'danger';
    if (absIc >= 0.03 && pValue < 0.05) return 'success';
    if (absIc >= 0.02) return 'warn';
    return 'info';
  }

  formatIc(ic: number): string {
    return ic.toFixed(4);
  }

  formatPValue(p: number): string {
    if (p < 0.001) return '<0.001';
    return p.toFixed(3);
  }

  isBestHorizon(horizon: number): boolean {
    return this.result()?.best_horizon === horizon;
  }

  // ─── Chart Rendering ─────────────────────────────────────

  private renderIcChart(canvas: HTMLCanvasElement, res: IndicatorReliabilityResponse): void {
    if (this.icChart) this.icChart.destroy();

    const meanIc =
      res.daily_ic_values.reduce((a, b) => a + b, 0) / res.daily_ic_values.length || 0;
    const meanLine = res.daily_ic_dates.map(() => meanIc);
    const zeroLine = res.daily_ic_dates.map(() => 0);

    this.icChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.daily_ic_dates,
        datasets: [
          {
            label: 'Daily IC',
            data: res.daily_ic_values,
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59, 130, 246, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 2,
            pointHoverRadius: 5,
            borderWidth: 2,
          },
          {
            label: `Mean IC (${meanIc.toFixed(4)})`,
            data: meanLine,
            borderColor: '#f97316',
            borderDash: [6, 4],
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Zero',
            data: zeroLine,
            borderColor: '#cbd5e1',
            borderDash: [3, 3],
            pointRadius: 0,
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true,
            text: `Daily IC Time Series (${res.display_name} at ${res.best_horizon ?? res.results[0]?.horizon ?? 10}-bar horizon)`,
            font: { size: 15, weight: 'bold' },
            color: '#1e293b',
            padding: { bottom: 16 },
          },
          legend: {
            position: 'bottom',
            labels: {
              font: { size: 12 },
              color: '#475569',
              padding: 16,
              usePointStyle: true,
              filter: item => item.text !== 'Zero',
            },
          },
          tooltip: {
            backgroundColor: '#1e293b',
            titleFont: { size: 13 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: ctx => {
                if (ctx.dataset.label === 'Zero') return '';
                return `${ctx.dataset.label}: ${Number(ctx.raw).toFixed(4)}`;
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 'IC (Spearman \u03C1)',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: { font: { size: 12 }, color: '#64748b' },
            grid: { color: '#f1f5f9' },
          },
          x: {
            title: {
              display: true,
              text: 'Date',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: {
              font: { size: 11 },
              color: '#64748b',
              maxRotation: 45,
              maxTicksLimit: 12,
            },
            grid: { display: false },
          },
        },
      },
    });
  }
}
