import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { DataViewModule } from 'primeng/dataview';
import { InputTextModule } from 'primeng/inputtext';
import { TagModule } from 'primeng/tag';

import type {
  BotCatalogRow,
  BotCatalogTradingMode,
  BotCatalogTone,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { fmtInteger, fmtSignedCurrency, fmtTimestampLocal } from '../format';

type ErrorFilter = 'all' | 'has-errors' | 'no-errors';
type TradingModeFilter = 'all' | BotCatalogTradingMode;
type TagSeverity = 'success' | 'warn' | 'danger' | 'secondary';

@Component({
  selector: 'app-bots-page',
  imports: [
    CommonModule,
    FormsModule,
    ButtonModule,
    DataViewModule,
    InputTextModule,
    TagModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bots-page.component.html',
  styleUrl: './bots-page.component.scss',
})
export class BotsPageComponent {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly router = inject(Router);

  readonly bots = signal<BotCatalogRow[]>([]);
  readonly expanded = signal<Record<string, boolean>>({});
  readonly isLoading = signal<boolean>(true);
  readonly errorMessage = signal<string | null>(null);
  readonly nameQuery = signal<string>('');
  readonly symbolQuery = signal<string>('');
  readonly errorFilter = signal<ErrorFilter>('all');
  readonly tradingModeFilter = signal<TradingModeFilter>('all');

  readonly visibleBots = computed<BotCatalogRow[]>(() => {
    const nameQuery = normalize(this.nameQuery());
    const symbolQuery = normalize(this.symbolQuery());
    const errorFilter = this.errorFilter();
    const tradingModeFilter = this.tradingModeFilter();

    return this.bots().filter((bot) => {
      if (nameQuery && !normalize(bot.name).includes(nameQuery)) return false;
      if (
        symbolQuery &&
        !bot.symbols.some((symbol) => normalize(symbol).includes(symbolQuery))
      ) {
        return false;
      }
      if (errorFilter === 'has-errors' && !bot.needs_attention) return false;
      if (errorFilter === 'no-errors' && bot.needs_attention) return false;
      if (tradingModeFilter !== 'all' && bot.trading_mode !== tradingModeFilter) return false;
      return true;
    });
  });

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.isLoading.set(true);
    this.errorMessage.set(null);
    try {
      const catalog = await this.liveRuns.getBotCatalog();
      this.bots.set(catalog.bots);
    } catch (err) {
      this.errorMessage.set(this.humanError(err));
    } finally {
      this.isLoading.set(false);
    }
  }

  toggleExpanded(id: string): void {
    this.expanded.update((value) => ({ ...value, [id]: !value[id] }));
  }

  setErrorFilter(value: ErrorFilter): void {
    this.errorFilter.set(value);
  }

  setTradingModeFilter(value: TradingModeFilter): void {
    this.tradingModeFilter.set(value);
  }

  isExpanded(id: string): boolean {
    return this.expanded()[id] === true;
  }

  async openBot(id: string): Promise<void> {
    await this.router.navigate(['/broker/bots', id]);
  }

  tagSeverity(tone: BotCatalogTone): TagSeverity {
    switch (tone) {
      case 'positive':
        return 'success';
      case 'warning':
        return 'warn';
      case 'danger':
        return 'danger';
      case 'neutral':
        return 'secondary';
    }
  }

  formatMoney(value: number | null): string {
    return fmtSignedCurrency(value);
  }

  formatCount(value: number | null): string {
    return fmtInteger(value);
  }

  formatTimestamp(value: number | null): string {
    return fmtTimestampLocal(value);
  }

  formatSymbols(symbols: string[]): string {
    return symbols.length > 0 ? symbols.join(', ') : '—';
  }

  private humanError(err: unknown): string {
    if (err instanceof Error && err.message) return err.message;
    return 'Could not load bots.';
  }
}

function normalize(value: string): string {
  return value.trim().toLowerCase();
}
