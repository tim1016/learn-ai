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
import { catchError, of, finalize, interval, switchMap, EMPTY, Subscription } from 'rxjs';
import { ResearchService, SignalEngineResult } from '../../../services/research.service';
import { MarketDataService } from '../../../services/market-data.service';
import { FetchProgress } from '../../../graphql/types';
import { SignalReportComponent } from '../signal-report/signal-report.component';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ProgressSpinner } from 'primeng/progressspinner';
import { ToggleSwitch } from 'primeng/toggleswitch';

interface FeatureOption {
  label: string;
  value: string;
}

@Component({
  selector: 'app-signal-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    SignalReportComponent,
    Select,
    InputText,
    ButtonModule,
    MessageModule,
    ProgressSpinner,
    ToggleSwitch,
  ],
  templateUrl: './signal-runner.component.html',
  styleUrls: ['./signal-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalRunnerComponent {
  private researchService = inject(ResearchService);
  private marketDataService = inject(MarketDataService);
  private destroyRef = inject(DestroyRef);

  // Form inputs
  ticker = signal('AAPL');
  featureName = signal('momentum_5m');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-06-30');
  flipSign = signal(true);
  regimeGateEnabled = signal(true);
  forceRefresh = signal(false);

  // State
  loading = signal(false);
  result = signal<SignalEngineResult | null>(null);
  error = signal<string | null>(null);
  formCollapsed = signal(false);
  fetchProgress = signal<FetchProgress | null>(null);
  private progressSub: Subscription | null = null;

  features: FeatureOption[] = [
    { label: '5-Minute Momentum', value: 'momentum_5m' },
    { label: 'RSI (14)', value: 'rsi_14' },
    { label: 'Realized Volatility (30)', value: 'realized_vol_30' },
    { label: 'Volume Z-Score', value: 'volume_zscore' },
    { label: 'MACD Signal', value: 'macd_signal' },
  ];

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.featureName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
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

  runSignalEngine(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    this.fetchProgress.set(null);

    this.startProgressPolling();

    this.researchService
      .runSignalEngine({
        ticker: this.ticker().toUpperCase(),
        featureName: this.featureName(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        flipSign: this.flipSign(),
        regimeGateEnabled: this.regimeGateEnabled(),
        forceRefresh: this.forceRefresh(),
      })
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'An unexpected error occurred');
          return of(null);
        }),
        finalize(() => {
          this.stopProgressPolling();
          this.loading.set(false);
        }),
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

  private startProgressPolling(): void {
    this.stopProgressPolling();
    this.progressSub = interval(2000)
      .pipe(
        switchMap(() => this.marketDataService.getFetchProgress(this.ticker().toUpperCase())
          .pipe(catchError(() => EMPTY))
        ),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe(progress => {
        this.fetchProgress.set(progress);
      });
  }

  private stopProgressPolling(): void {
    this.progressSub?.unsubscribe();
    this.progressSub = null;
    this.fetchProgress.set(null);
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
