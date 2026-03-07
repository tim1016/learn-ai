import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { ScenarioResult } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-scenario-explorer',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './scenario-explorer.component.html',
  styleUrls: ['./scenario-explorer.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ScenarioExplorerComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  priceChange = signal<number | null>(-10);
  ivChange = signal<number | null>(null);
  thetaDays = signal<number | null>(null);
  result = signal<ScenarioResult | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  presets = [
    { label: 'Market Crash (-20%)', price: -20, iv: 15, days: null },
    { label: 'Correction (-10%)', price: -10, iv: 5, days: null },
    { label: 'Rally (+10%)', price: 10, iv: -5, days: null },
    { label: 'Vol Spike (IV +20%)', price: -5, iv: 20, days: null },
    { label: 'Theta Decay (5 days)', price: null, iv: null, days: 5 },
    { label: 'Theta Decay (30 days)', price: null, iv: null, days: 30 },
  ];

  applyPreset(preset: { price: number | null; iv: number | null; days: number | null }): void {
    this.priceChange.set(preset.price);
    this.ivChange.set(preset.iv);
    this.thetaDays.set(preset.days);
  }

  runScenario(): void {
    this.loading.set(true);
    this.error.set(null);

    const priceChangePercent = this.priceChange() != null ? this.priceChange()! / 100 : undefined;
    const ivChangePercent = this.ivChange() != null ? this.ivChange()! / 100 : undefined;
    const timeDaysForward = this.thetaDays() ?? undefined;

    this.portfolioService.runScenario(
      this.accountId(), [], priceChangePercent, ivChangePercent, timeDaysForward,
    ).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(r => { if (r) this.result.set(r); });
  }
}
