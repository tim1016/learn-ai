import {
  Component,
  signal,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { ResearchService, SignalEngineResult } from '../../../services/research.service';
import { SignalReportComponent } from '../signal-report/signal-report.component';
import { ProgressSpinnerModule } from 'primeng/progressspinner';
import { MessageModule } from 'primeng/message';

@Component({
  selector: 'app-signal-report-page',
  standalone: true,
  imports: [CommonModule, SignalReportComponent, ProgressSpinnerModule, MessageModule],
  templateUrl: './signal-report-page.component.html',
  styleUrls: ['./signal-report-page.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalReportPageComponent implements OnInit {
  private route = inject(ActivatedRoute);
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  loading = signal(true);
  result = signal<SignalEngineResult | null>(null);
  error = signal<string | null>(null);

  ngOnInit(): void {
    const id = Number(this.route.snapshot.paramMap.get('id'));
    if (isNaN(id)) {
      this.error.set('Invalid experiment ID');
      this.loading.set(false);
      return;
    }

    this.researchService
      .getSignalExperimentReport(id)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'Failed to load signal report');
          return of(null);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(report => {
        if (report) {
          this.result.set(report);
        } else if (!this.error()) {
          this.error.set('Signal experiment not found');
        }
      });
  }
}
