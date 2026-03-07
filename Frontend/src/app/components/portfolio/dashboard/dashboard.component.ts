import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef, effect } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of, forkJoin } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { PortfolioState, PortfolioValuation, PortfolioMetrics } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './dashboard.component.html',
  styleUrls: ['./dashboard.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DashboardComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  state = signal<PortfolioState | null>(null);
  valuation = signal<PortfolioValuation | null>(null);
  metrics = signal<PortfolioMetrics | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);
  snapshotMsg = signal<string | null>(null);

  // Trade form
  tradeSymbol = signal('');
  tradeSide = signal('Buy');
  tradeQty = signal(100);
  tradePrice = signal(0);
  tradeFees = signal(0);
  recording = signal(false);

  get openPositionCount(): number {
    return this.state()?.positions.filter(p => p.status === 'Open').length ?? 0;
  }

  constructor() {
    effect(() => {
      const id = this.accountId();
      if (id) this.loadDashboard();
    });
  }

  loadDashboard(): void {
    this.loading.set(true);
    this.error.set(null);
    const id = this.accountId();

    forkJoin({
      state: this.portfolioService.getPortfolioState(id),
      metrics: this.portfolioService.getMetrics(id).pipe(catchError(() => of(null))),
    }).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(result => {
      if (result) {
        this.state.set(result.state ?? null);
        this.metrics.set(result.metrics ?? null);
      }
    });
  }

  takeSnapshot(): void {
    this.snapshotMsg.set(null);
    this.portfolioService.takeSnapshot(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
    ).subscribe(res => {
      this.snapshotMsg.set(res.success ? res.message ?? 'Snapshot taken' : res.error ?? 'Failed');
    });
  }

  recordTrade(): void {
    if (!this.tradeSymbol() || !this.tradePrice()) return;
    this.recording.set(true);
    this.portfolioService.recordTrade(
      this.accountId(), this.tradeSymbol(), this.tradeSide(),
      this.tradeQty(), this.tradePrice(), this.tradeFees(),
    ).pipe(
      takeUntilDestroyed(this.destroyRef),
      finalize(() => this.recording.set(false)),
    ).subscribe(res => {
      if (res.success) this.loadDashboard();
      else this.error.set(res.error ?? 'Trade failed');
    });
  }
}
