import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { ReconciliationReport } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-reconciliation',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './reconciliation.component.html',
  styleUrls: ['./reconciliation.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReconciliationComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  report = signal<ReconciliationReport | null>(null);
  loading = signal(false);
  fixing = signal(false);
  error = signal<string | null>(null);
  fixMessage = signal<string | null>(null);

  runReconciliation(): void {
    this.loading.set(true);
    this.error.set(null);
    this.fixMessage.set(null);
    this.portfolioService.reconcile(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(r => { if (r) this.report.set(r); });
  }

  autoFix(): void {
    this.fixing.set(true);
    this.portfolioService.autoFix(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.fixing.set(false)),
    ).subscribe(res => {
      if (res?.success) {
        this.fixMessage.set(res.message ?? 'Positions rebuilt successfully');
        this.runReconciliation(); // Re-check
      } else {
        this.error.set(res?.error ?? 'Auto-fix failed');
      }
    });
  }
}
