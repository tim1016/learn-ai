import {
  Component,
  signal,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, tap } from 'rxjs';
import { RouterModule } from '@angular/router';
import { LstmService } from '../../../services/lstm.service';
import { LstmModelInfo } from '../../../graphql/lstm-types';

@Component({
  selector: 'app-lstm-predictions',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterModule,
  ],
  templateUrl: './lstm-predictions.component.html',
  styleUrls: ['./lstm-predictions.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LstmPredictionsComponent implements OnInit {
  private lstmService = inject(LstmService);
  private destroyRef = inject(DestroyRef);

  models = signal<LstmModelInfo[]>([]);
  selectedModelId = signal<string | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  // The train result from the last training job for the selected model
  // For now, we show model metadata; future: re-run inference to get predictions
  selectedModel = signal<LstmModelInfo | null>(null);

  ngOnInit(): void {
    this.loadModels();
  }

  loadModels(): void {
    this.loading.set(true);
    this.lstmService
      .getModels()
      .pipe(
        tap((models) => {
          this.models.set(models);
          this.loading.set(false);
        }),
        catchError((err) => {
          this.error.set(err?.message || 'Failed to load models');
          this.loading.set(false);
          return of(null);
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe();
  }

  selectModel(modelId: string): void {
    this.selectedModelId.set(modelId);
    const model = this.models().find((m) => m.modelId === modelId) ?? null;
    this.selectedModel.set(model);
  }
}
