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
import { LstmService } from '../../../services/lstm.service';
import { LstmTrainResult } from '../../../graphql/lstm-types';
import { PredictionChartComponent } from '../charts/prediction-chart.component';
import { TrainingHistoryChartComponent } from '../charts/training-history-chart.component';
import { ResidualsChartComponent } from '../charts/residuals-chart.component';

@Component({
  selector: 'app-lstm-train',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    PredictionChartComponent,
    TrainingHistoryChartComponent,
    ResidualsChartComponent,
  ],
  templateUrl: './lstm-train.component.html',
  styleUrls: ['./lstm-train.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LstmTrainComponent {
  private lstmService = inject(LstmService);
  private destroyRef = inject(DestroyRef);

  // Form inputs
  ticker = signal('AAPL');
  fromDate = signal('2023-01-01');
  toDate = signal('2024-12-31');
  epochs = signal(50);
  sequenceLength = signal(60);
  features = signal('close');
  mock = signal(false);

  // State
  loading = signal(false);
  jobId = signal<string | null>(null);
  status = signal<string | null>(null);
  error = signal<string | null>(null);
  result = signal<LstmTrainResult | null>(null);

  // Computed
  improvementPositive = computed(() => {
    const r = this.result();
    return r ? r.improvement > 0 : false;
  });

  featureOptions = ['close', 'close,volume', 'close,volume,high,low', 'open,high,low,close,volume'];

  startTraining(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    this.status.set('submitting');

    this.lstmService
      .startTraining({
        ticker: this.ticker(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        epochs: this.epochs(),
        sequenceLength: this.sequenceLength(),
        features: this.features(),
        mock: this.mock(),
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
          this.error.set(err?.message || 'Failed to submit training job');
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

          if (jobStatus.status === 'completed' && jobStatus.trainResult) {
            this.result.set(jobStatus.trainResult);
            this.loading.set(false);
          } else if (jobStatus.status === 'failed') {
            this.error.set(jobStatus.error || 'Training failed');
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
