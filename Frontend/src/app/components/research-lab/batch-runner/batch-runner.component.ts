import { Component, ChangeDetectionStrategy, signal, computed, inject, DestroyRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';

import { ButtonModule } from 'primeng/button';
import { SelectModule } from 'primeng/select';
import { InputTextModule } from 'primeng/inputtext';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { ProgressBarModule } from 'primeng/progressbar';
import { MessageModule } from 'primeng/message';
import { CheckboxModule } from 'primeng/checkbox';
import { CardModule } from 'primeng/card';

import {
  ResearchService,
  BatchResearchResult,
  TickerBatchResult,
} from '../../../services/research.service';

const DEFAULT_TICKERS = [
  'SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA',
  'TSLA', 'AMD', 'META', 'AMZN', 'NFLX',
  'JPM', 'XOM', 'BAC', 'IWM', 'DIA',
];

const OPTIONS_FEATURES = [
  { label: 'IV 30-Day ATM', value: 'iv_30d' },
  { label: 'IV Rank (60-Day)', value: 'iv_rank_60' },
  { label: 'Log Put-Call Skew', value: 'log_skew' },
  { label: 'IV Rank (252-Day)', value: 'iv_rank_252' },
  { label: 'VRP (5-Day)', value: 'vrp_5' },
];

const TARGET_TYPES = [
  { label: 'Directional (1d forward return)', value: 'directional' },
  { label: 'Volatility (5d forward RV)', value: 'volatility' },
  { label: 'Absolute Return', value: 'abs_return' },
];

@Component({
  selector: 'app-batch-runner',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    ButtonModule, SelectModule, InputTextModule,
    TableModule, TagModule, ProgressBarModule,
    MessageModule, CheckboxModule, CardModule,
  ],
  templateUrl: './batch-runner.component.html',
  styleUrl: './batch-runner.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BatchRunnerComponent {
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  featureName = signal('iv_rank_60');
  fromDate = signal('2024-06-01');
  toDate = signal('2026-02-28');
  targetType = signal('directional');
  loading = signal(false);
  result = signal<BatchResearchResult | null>(null);
  error = signal<string | null>(null);

  selectedTickers = signal<string[]>([...DEFAULT_TICKERS]);

  readonly features = OPTIONS_FEATURES;
  readonly targetTypes = TARGET_TYPES;
  readonly allTickers = DEFAULT_TICKERS;

  get passRatePct(): number {
    return (this.result()?.passRate ?? 0) * 100;
  }

  get consistentSeverity(): 'success' | 'danger' {
    return this.result()?.crossSectionalConsistent ? 'success' : 'danger';
  }

  get consistentLabel(): string {
    return this.result()?.crossSectionalConsistent ? 'Consistent' : 'Not Consistent';
  }

  toggleTicker(ticker: string): void {
    const current = this.selectedTickers();
    if (current.includes(ticker)) {
      this.selectedTickers.set(current.filter(t => t !== ticker));
    } else {
      this.selectedTickers.set([...current, ticker]);
    }
  }

  selectAll(): void {
    this.selectedTickers.set([...DEFAULT_TICKERS]);
  }

  deselectAll(): void {
    this.selectedTickers.set([]);
  }

  runBatch(): void {
    if (this.selectedTickers().length === 0) {
      this.error.set('Select at least one ticker');
      return;
    }

    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    this.researchService
      .runBatchOptionsResearch({
        featureName: this.featureName(),
        tickers: this.selectedTickers(),
        fromDate: this.fromDate(),
        toDate: this.toDate(),
        targetType: this.targetType(),
      })
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'Batch research failed');
          return of(null);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(res => {
        if (res) {
          this.result.set(res);
          if (res.error) {
            this.error.set(res.error);
          }
        }
      });
  }

  getValidationSeverity(passed: boolean): 'success' | 'danger' {
    return passed ? 'success' : 'danger';
  }

  getIcClass(ic: number): string {
    const abs = Math.abs(ic);
    if (abs >= 0.05) return 'text-green-500 font-bold';
    if (abs >= 0.03) return 'text-yellow-500';
    return 'text-red-500';
  }
}
