import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { TableModule } from 'primeng/table';
import { TabsModule } from 'primeng/tabs';
import { TagModule } from 'primeng/tag';

import type {
  BotCatalogRow,
  BotCatalogTradingMode,
  ReadinessVerdictEnum,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { fmtInteger, fmtSignedCurrency, fmtTimestampLocal } from '../format';

type AttentionFilter = 'all' | 'needs-attention' | 'healthy';
type BotModeTab = BotCatalogTradingMode;
type ReadinessFilter = 'all' | ReadinessVerdictEnum;
type TagSeverity = 'success' | 'warn' | 'danger' | 'secondary';

interface BotTableRow {
  id: string;
  name: string;
  needsAttention: boolean;
  tradingMode: BotCatalogTradingMode;
  symbolsLabel: string;
  readinessVerdict: ReadinessVerdictEnum;
  exposure: string;
  openPositions: number | null;
  totalPnl: number | null;
  errorCount: number;
  lastRunSortMs: number;
  lastRunAtMs: number | null;
  lastRunLabel: string;
  searchText: string;
}

@Component({
  selector: 'app-bots-page',
  imports: [
    CommonModule,
    ButtonModule,
    InputTextModule,
    TableModule,
    TabsModule,
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
  readonly isLoading = signal<boolean>(true);
  readonly errorMessage = signal<string | null>(null);
  readonly searchQuery = signal<string>('');
  readonly attentionFilter = signal<AttentionFilter>('all');
  readonly readinessFilter = signal<ReadinessFilter>('all');
  readonly activeModeTab = signal<BotModeTab>('paper');
  readonly selectedBotIds = signal<ReadonlySet<string>>(new Set<string>());
  readonly deleteConfirmationOpen = signal<boolean>(false);
  readonly isDeleting = signal<boolean>(false);
  readonly deleteErrorMessage = signal<string | null>(null);

  readonly visibleBots = computed<BotTableRow[]>(() => {
    const query = normalize(this.searchQuery());
    const attentionFilter = this.attentionFilter();
    const readinessFilter = this.readinessFilter();

    return this.bots()
      .map(toTableRow)
      .filter((row) => {
        if (query && !row.searchText.includes(query)) return false;
        if (attentionFilter === 'needs-attention' && !row.needsAttention) return false;
        if (attentionFilter === 'healthy' && row.needsAttention) return false;
        if (readinessFilter !== 'all' && row.readinessVerdict !== readinessFilter) return false;
        return true;
      })
      .sort(compareRowsForTriage);
  });

  readonly liveBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'live'),
  );

  readonly paperBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'paper'),
  );

  readonly unknownModeBots = computed<BotTableRow[]>(() =>
    this.visibleBots().filter((bot) => bot.tradingMode === 'unknown'),
  );

  readonly activeRows = computed<BotTableRow[]>(() => {
    switch (this.activeModeTab()) {
      case 'live':
        return this.liveBots();
      case 'paper':
        return this.paperBots();
      case 'unknown':
        return this.unknownModeBots();
    }
  });

  readonly activeTabCount = computed(() => this.activeRows().length);

  readonly selectedRows = computed<BotTableRow[]>(() => {
    const selected = this.selectedBotIds();
    return this.visibleBots().filter((row) => selected.has(row.id));
  });

  readonly selectedNamesLabel = computed(() =>
    this.selectedRows().map((row) => row.name).join(', '),
  );

  readonly selectedCount = computed(() => this.selectedBotIds().size);

  readonly allActiveRowsSelected = computed(() => {
    const rows = this.activeRows();
    return rows.length > 0 && rows.every((row) => this.selectedBotIds().has(row.id));
  });

  readonly someActiveRowsSelected = computed(() => {
    const rows = this.activeRows();
    return rows.some((row) => this.selectedBotIds().has(row.id));
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
      this.retainKnownSelections(catalog.bots);
    } catch (err) {
      this.errorMessage.set(this.humanError(err));
    } finally {
      this.isLoading.set(false);
    }
  }

  setSearchQuery(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.searchQuery.set(target.value);
    }
  }

  setAttentionFilter(value: AttentionFilter): void {
    this.attentionFilter.set(value);
  }

  setReadinessFilter(value: ReadinessFilter): void {
    this.readinessFilter.set(value);
  }

  setActiveModeTab(value: string | number | undefined): void {
    if (value === 'live' || value === 'paper' || value === 'unknown') {
      this.activeModeTab.set(value);
    }
  }

  clearFilters(): void {
    this.searchQuery.set('');
    this.attentionFilter.set('all');
    this.readinessFilter.set('all');
  }

  isSelected(id: string): boolean {
    return this.selectedBotIds().has(id);
  }

  setBotSelection(id: string, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.toggleBotSelection(id, target.checked);
    }
  }

  setActiveTabSelection(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    const ids = this.activeRows().map((row) => row.id);
    this.selectedBotIds.update((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (target.checked) {
          next.add(id);
        } else {
          next.delete(id);
        }
      }
      return next;
    });
    this.closeDeleteConfirmation();
  }

  toggleBotSelection(id: string, selected = !this.isSelected(id)): void {
    this.selectedBotIds.update((current) => {
      const next = new Set(current);
      if (selected) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
    this.closeDeleteConfirmation();
  }

  clearSelection(): void {
    this.selectedBotIds.set(new Set<string>());
    this.closeDeleteConfirmation();
  }

  requestDeleteSelected(): void {
    if (this.selectedCount() === 0 || this.isDeleting()) return;
    this.deleteErrorMessage.set(null);
    this.deleteConfirmationOpen.set(true);
  }

  cancelDeleteSelected(): void {
    this.closeDeleteConfirmation();
  }

  async confirmDeleteSelected(): Promise<void> {
    const ids = [...this.selectedBotIds()];
    if (ids.length === 0 || this.isDeleting()) return;
    this.isDeleting.set(true);
    this.deleteErrorMessage.set(null);
    try {
      await Promise.all(
        ids.map((id) =>
          this.liveRuns.deleteBot(id, {
            mode: 'soft',
            deleted_by: 'operator',
            reason: 'Deleted from Bots page',
          }),
        ),
      );
      this.selectedBotIds.set(new Set<string>());
      this.deleteConfirmationOpen.set(false);
      await this.refresh();
    } catch (err) {
      this.deleteErrorMessage.set(this.humanError(err));
    } finally {
      this.isDeleting.set(false);
    }
  }

  async openBot(id: string): Promise<void> {
    await this.router.navigate(['/broker/bots', id]);
  }

  readinessSeverity(verdict: ReadinessVerdictEnum): TagSeverity {
    switch (verdict) {
      case 'READY':
        return 'success';
      case 'DEGRADED':
        return 'warn';
      case 'BLOCKED':
        return 'danger';
      case 'UNKNOWN':
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

  readonly trackByBotId = (_index: number, row: BotTableRow): string => row.id;

  private closeDeleteConfirmation(): void {
    this.deleteConfirmationOpen.set(false);
    this.deleteErrorMessage.set(null);
  }

  private retainKnownSelections(bots: BotCatalogRow[]): void {
    const knownIds = new Set(bots.map((bot) => bot.strategy_instance_id));
    this.selectedBotIds.update((current) => {
      const next = new Set([...current].filter((id) => knownIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }

  private humanError(err: unknown): string {
    if (err instanceof Error && err.message) return err.message;
    return 'Could not load bots.';
  }
}

function normalize(value: string): string {
  return value.trim().toLowerCase();
}

function toTableRow(bot: BotCatalogRow): BotTableRow {
  const symbolsLabel = bot.symbols.length > 0 ? bot.symbols.join(', ') : '—';

  return {
    id: bot.strategy_instance_id,
    name: bot.name,
    needsAttention: bot.needs_attention,
    tradingMode: bot.trading_mode,
    symbolsLabel,
    readinessVerdict: bot.readiness_verdict,
    exposure: bot.metrics.current_exposure,
    openPositions: bot.metrics.open_positions,
    totalPnl: bot.metrics.pnl.total,
    errorCount: bot.metrics.error_count,
    lastRunSortMs: bot.last_run_at_ms ?? 0,
    lastRunAtMs: bot.last_run_at_ms,
    lastRunLabel: bot.last_run_label,
    searchText: normalize([
      bot.name,
      bot.strategy_instance_id,
      symbolsLabel,
      bot.status_label,
      bot.status_detail,
      bot.trading_mode,
      bot.engine,
      bot.engine_asset_class,
      bot.desired_state,
      bot.readiness_verdict,
      bot.last_run_label,
      bot.last_run_result,
      bot.last_run_detail,
      bot.metrics.current_exposure,
    ].filter((value): value is string => typeof value === 'string').join(' ')),
  };
}

function compareRowsForTriage(a: BotTableRow, b: BotTableRow): number {
  return (
    b.lastRunSortMs - a.lastRunSortMs ||
    a.name.localeCompare(b.name)
  );
}
