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
import { catchError, of, tap } from 'rxjs';
import { TableModule } from 'primeng/table';
import { LstmService } from '../../../services/lstm.service';
import { LstmValidateResult } from '../../../graphql/lstm-types';
import { FoldMetricsChartComponent } from '../charts/fold-metrics-chart.component';

@Component({
  selector: 'app-lstm-validate',
  standalone: true,
  imports: [CommonModule, FormsModule, TableModule, FoldMetricsChartComponent],
  templateUrl: './lstm-validate.component.html',
  styleUrls: ['./lstm-validate.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LstmValidateComponent {
  private lstmService = inject(LstmService);
  private destroyRef = inject(DestroyRef);

  // Form
  ticker = signal('AAPL');
  fromDate = signal('2023-01-01');
  toDate = signal('2024-12-31');
  folds = signal(5);
  epochs = signal(20);
  sequenceLength = signal(60);
  mock = signal(false);
  scalerType = signal('standard');
  logReturns = signal(false);
  winsorize = signal(false);
  timespan = signal('day');
  multiplier = signal(1);

  // UI state
  showHelp = signal(false);

  // State
  loading = signal(false);
  jobId = signal<string | null>(null);
  status = signal<string | null>(null);
  error = signal<string | null>(null);
  result = signal<LstmValidateResult | null>(null);

  dataPointEstimate = computed(() => {
    const from = new Date(this.fromDate());
    const to = new Date(this.toDate());
    const days = Math.max(0, (to.getTime() - from.getTime()) / (1000 * 60 * 60 * 24));
    const tradingDays = Math.round(days * (252 / 365));
    const ts = this.timespan();
    const mult = this.multiplier();

    if (ts === 'day') return Math.round(tradingDays / mult);
    if (ts === 'hour') return Math.round((tradingDays * 6.5) / mult);
    if (ts === 'minute') return Math.round((tradingDays * 390) / mult);
    if (ts === 'week') return Math.round(tradingDays / (5 * mult));
    return tradingDays;
  });

  scalerOptions = [
    { value: 'standard', label: 'Standard (z-score)' },
    { value: 'robust', label: 'Robust (median/IQR)' },
    { value: 'minmax', label: 'MinMax [0,1]' },
  ];

  timespanOptions = [
    { value: 'minute', label: 'Minute' },
    { value: 'hour', label: 'Hour' },
    { value: 'day', label: 'Day' },
    { value: 'week', label: 'Week' },
  ];

  startValidation(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    this.status.set('submitting');

    this.lstmService
      .startValidation({
        ticker: this.ticker(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        folds: this.folds(),
        epochs: this.epochs(),
        sequenceLength: this.sequenceLength(),
        mock: this.mock(),
        scalerType: this.scalerType(),
        logReturns: this.logReturns(),
        winsorize: this.winsorize(),
        timespan: this.timespan(),
        multiplier: this.multiplier(),
      })
      .pipe(
        tap((jobResult) => {
          if (!jobResult.success) {
            this.error.set(jobResult.message);
            this.loading.set(false);
            return;
          }
          this.jobId.set(jobResult.jobId);
          this.status.set('running');
          this.pollForResult(jobResult.jobId);
        }),
        catchError((err) => {
          this.error.set(err?.message || 'Failed to submit validation job');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe();
  }

  private pollForResult(jobId: string): void {
    this.lstmService
      .pollJob(jobId)
      .pipe(
        tap((jobStatus) => {
          this.status.set(jobStatus.status);

          if (jobStatus.status === 'completed' && jobStatus.validateResult) {
            this.result.set(jobStatus.validateResult);
            this.loading.set(false);
          } else if (jobStatus.status === 'failed') {
            this.error.set(jobStatus.error || 'Validation failed');
            this.loading.set(false);
          }
        }),
        catchError((err) => {
          this.error.set(err?.message || 'Error polling job status');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe();
  }
}
