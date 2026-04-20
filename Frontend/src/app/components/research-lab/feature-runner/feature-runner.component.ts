import {
  Component,
  signal,
  computed,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { ResearchService, ResearchResult } from '../../../services/research.service';
import { FeatureReportComponent } from '../feature-report/feature-report.component';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ProgressSpinner } from 'primeng/progressspinner';

interface FeatureOption {
  label: string;
  value: string;
}

@Component({
  selector: 'app-feature-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    FeatureReportComponent,
    Select,
    InputText,
    ButtonModule,
    MessageModule,
    ProgressSpinner,
  ],
  templateUrl: './feature-runner.component.html',
  styleUrls: ['./feature-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureRunnerComponent {
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  // Form inputs
  ticker = signal('AAPL');
  featureName = signal('momentum_5m');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-03-31');
  timespan = signal('minute');
  multiplier = signal(1);

  // State
  loading = signal(false);
  result = signal<ResearchResult | null>(null);
  error = signal<string | null>(null);
  formCollapsed = signal(false);

  features: FeatureOption[] = [
    { label: '5-Minute Momentum', value: 'momentum_5m' },
    { label: 'RSI (14)', value: 'rsi_14' },
    { label: 'Realized Volatility (30)', value: 'realized_vol_30' },
    { label: 'Volume Z-Score', value: 'volume_zscore' },
    { label: 'MACD Signal', value: 'macd_signal' },
  ];

  timespanOptions: FeatureOption[] = [
    { label: 'Minute', value: 'minute' },
    { label: 'Hour', value: 'hour' },
    { label: 'Day', value: 'day' },
  ];

  // ── Range guard ─────────────────────────────────────
  // Minute-resolution research at 2 years exceeds ~200k bars, which overruns
  // the backend→python keep-alive window and makes Polly retry for the full
  // 10-minute HttpClient timeout before failing the GraphQL mutation. Cap
  // minute studies to 180 calendar days; hour at 3 years; day unrestricted.
  private readonly MAX_DAYS_BY_TIMESPAN: Readonly<Record<string, number>> = {
    minute: 180,
    hour: 1095,
    day: Number.POSITIVE_INFINITY,
  };

  readonly maxRangeDays = computed<number>(() =>
    this.MAX_DAYS_BY_TIMESPAN[this.timespan()] ?? 180,
  );

  readonly rangeDays = computed<number | null>(() => {
    const from = Date.parse(this.fromDate());
    const to = Date.parse(this.toDate());
    if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null;
    return Math.round((to - from) / 86_400_000);
  });

  readonly rangeWarning = computed<string | null>(() => {
    const days = this.rangeDays();
    if (days === null) return null;
    const cap = this.maxRangeDays();
    if (days <= cap) return null;
    const capLabel = Number.isFinite(cap) ? `${cap} days` : 'no cap';
    return `${this.timespan()}-resolution research is capped at ${capLabel} (you selected ${days} days). Long minute-bar requests don't survive the backend→python connection window. Shorten the range, or switch to an hourly/daily timespan.`;
  });

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.featureName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
      this.rangeWarning() === null &&
      !this.loading()
    );
  });

  get selectedFeatureLabel(): string {
    const found = this.features.find(f => f.value === this.featureName());
    return found ? found.label : this.featureName();
  }

  get runSummary(): string {
    return `${this.selectedFeatureLabel} on ${this.ticker().toUpperCase()} (${this.fromDate()} to ${this.toDate()})`;
  }

  runResearch(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    this.researchService
      .runFeatureResearch({
        ticker: this.ticker().toUpperCase(),
        featureName: this.featureName(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        timespan: this.timespan(),
        multiplier: this.multiplier(),
      })
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'An unexpected error occurred');
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
}
