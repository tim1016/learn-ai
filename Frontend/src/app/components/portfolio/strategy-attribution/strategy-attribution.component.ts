import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef, effect } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { AlphaAttribution, StrategyAllocation, StrategyPnLResult } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-strategy-attribution',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './strategy-attribution.component.html',
  styleUrls: ['./strategy-attribution.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyAttributionComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  allocations = signal<StrategyAllocation[]>([]);
  attributions = signal<AlphaAttribution[]>([]);
  selectedPnL = signal<StrategyPnLResult | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  // Import form
  importStrategyId = signal<number>(0);
  importing = signal(false);
  importMsg = signal<string | null>(null);

  constructor() {
    effect(() => { if (this.accountId()) this.load(); });
  }

  load(): void {
    this.loading.set(true);
    this.portfolioService.getStrategyAllocations(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(a => this.allocations.set(a));
  }

  loadAttribution(): void {
    this.loading.set(true);
    this.portfolioService.getAlphaAttribution(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(a => this.attributions.set(a));
  }

  viewStrategyPnL(executionId: number): void {
    this.portfolioService.getStrategyPnL(executionId).pipe(
      takeUntilDestroyed(this.destroyRef),
    ).subscribe(r => this.selectedPnL.set(r));
  }

  importTrades(): void {
    const id = this.importStrategyId();
    if (!id) return;
    this.importing.set(true);
    this.importMsg.set(null);
    this.portfolioService.importBacktestTrades(id, this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.importing.set(false)),
    ).subscribe(res => {
      if (res?.success) {
        this.importMsg.set(res.message ?? `Imported ${res.tradeCount} trades`);
        this.load();
      } else {
        this.error.set(res?.error ?? 'Import failed');
      }
    });
  }

  Math = Math;

  get maxContribution(): number {
    return Math.max(...this.attributions().map(a => Math.abs(a.contributionPercent)), 0.01);
  }
}
