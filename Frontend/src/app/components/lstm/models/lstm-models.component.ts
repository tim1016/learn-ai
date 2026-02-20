import {
  Component,
  signal,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, tap } from 'rxjs';
import { TableModule } from 'primeng/table';
import { LstmService } from '../../../services/lstm.service';
import { LstmModelInfo } from '../../../graphql/lstm-types';

@Component({
  selector: 'app-lstm-models',
  standalone: true,
  imports: [CommonModule, TableModule],
  templateUrl: './lstm-models.component.html',
  styleUrls: ['./lstm-models.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LstmModelsComponent implements OnInit {
  private lstmService = inject(LstmService);
  private destroyRef = inject(DestroyRef);

  models = signal<LstmModelInfo[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);

  ngOnInit(): void {
    this.loadModels();
  }

  loadModels(): void {
    this.loading.set(true);
    this.error.set(null);

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

  getImprovementClass(improvement: number): string {
    return improvement > 0 ? 'positive-text' : 'negative-text';
  }
}
