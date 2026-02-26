import {
  Component,
  signal,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { ResearchService, ResearchExperiment } from '../../../services/research.service';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { MessageModule } from 'primeng/message';

@Component({
  selector: 'app-experiment-history',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    InputText,
    ButtonModule,
    TableModule,
    TagModule,
    MessageModule,
  ],
  templateUrl: './experiment-history.component.html',
  styleUrls: ['./experiment-history.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExperimentHistoryComponent {
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  ticker = signal('AAPL');
  loading = signal(false);
  experiments = signal<ResearchExperiment[]>([]);
  error = signal<string | null>(null);

  loadExperiments(): void {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.loading.set(true);
    this.error.set(null);

    this.researchService
      .getExperiments(t)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'Failed to load experiments');
          return of([]);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(exps => this.experiments.set(exps));
  }

  validationSeverity(passed: boolean): 'success' | 'danger' {
    return passed ? 'success' : 'danger';
  }

  validationLabel(passed: boolean): string {
    return passed ? 'PASSED' : 'FAILED';
  }
}
