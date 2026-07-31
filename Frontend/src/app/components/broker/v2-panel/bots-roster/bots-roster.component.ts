import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { formatReceiptLabel, ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtInteger, fmtSignedCurrency, fmtSignedQuantity } from '../../format';
import { BotStatusChipComponent } from '../bot-status-chip/bot-status-chip.component';
import type { ActionId, BotCatalogView, PanelProfile } from '../lib/broker-v2-panel.types';

export interface RowActionEvent {
  bot: BotCatalogView;
  action: ActionId;
}

type StatusFilter = 'all' | 'working' | 'off_duty' | 'attention' | 'retired';

interface StatusOption {
  readonly label: string;
  readonly value: StatusFilter;
}

@Component({
  selector: 'app-bots-roster',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe, BotStatusChipComponent, TimestampDisplayComponent],
  templateUrl: './bots-roster.component.html',
  styleUrl: './bots-roster.component.scss',
  host: { class: 'flex min-h-0 flex-1 flex-col' },
})
export class BotsRosterComponent {
  readonly bots = input.required<BotCatalogView[]>();
  readonly profile = input<PanelProfile | null>(null);
  readonly broker = input.required<string>();
  readonly accountId = input<string | undefined>(undefined);
  readonly pendingBotIds = input<ReadonlySet<string>>(new Set());
  readonly rowAction = output<RowActionEvent>();

  protected readonly searchTerm = signal('');
  protected readonly statusFilter = signal<StatusFilter>('all');
  protected readonly fmtSignedCurrency = fmtSignedCurrency;
  protected readonly fmtInteger = fmtInteger;

  protected readonly statusOptions: readonly StatusOption[] = [
    { label: 'All', value: 'all' },
    { label: 'Working', value: 'working' },
    { label: 'Off duty', value: 'off_duty' },
    { label: 'Needs attention', value: 'attention' },
    { label: 'Retired', value: 'retired' },
  ];

  protected readonly filtered = computed(() => {
    const term = this.searchTerm().toLowerCase().trim();
    const filter = this.statusFilter();
    const passed = this.bots().filter((bot) => {
      const strategyLabel = formatReceiptLabel(bot.strategy_key).toLowerCase();
      const matchesTerm =
        !term ||
        bot.strategy_instance_id.toLowerCase().includes(term) ||
        bot.symbol.toLowerCase().includes(term) ||
        bot.strategy_key.toLowerCase().includes(term) ||
        strategyLabel.includes(term);
      const matchesFilter =
        filter === 'all' ||
        (filter === 'working' && bot.running) ||
        (filter === 'off_duty' && !bot.running && bot.phase !== 'RETIRED') ||
        (filter === 'attention' && bot.needs_attention) ||
        (filter === 'retired' && bot.phase === 'RETIRED');
      return matchesTerm && matchesFilter;
    });

    return passed.sort((left, right) => {
      if (left.needs_attention !== right.needs_attention) {
        return left.needs_attention ? -1 : 1;
      }
      return (right.last_activity_at_ms ?? 0) - (left.last_activity_at_ms ?? 0);
    });
  });

  protected readonly emptyMessage = computed(() =>
    this.bots().length === 0
      ? 'No Alpaca bots yet. Deploy a strategy to create the first fleet member.'
      : 'No bots match these filters. Clear the search or choose another state.',
  );

  protected onSearch(event: Event): void {
    const target = event.target;
    this.searchTerm.set(target instanceof HTMLInputElement ? target.value : '');
  }

  protected selectFilter(filter: StatusFilter): void {
    this.statusFilter.set(filter);
  }

  protected exposureText(bot: BotCatalogView): string {
    const entries = Object.entries(bot.exposure);
    if (entries.length === 0) return 'Flat';
    return entries
      .map(([symbol, quantity]) => `${fmtSignedQuantity(quantity)} ${symbol}`)
      .join(', ');
  }

  protected canStart(bot: BotCatalogView): boolean {
    return bot.phase === 'OFF_DUTY' && this.supports('start');
  }

  protected canStop(bot: BotCatalogView): boolean {
    return bot.phase === 'ON_DUTY' && this.supports('stop');
  }

  protected isPending(bot: BotCatalogView): boolean {
    return this.pendingBotIds().has(bot.strategy_instance_id);
  }

  protected botLink(bot: BotCatalogView): readonly string[] {
    return [
      '/brokers',
      this.broker(),
      'accounts',
      this.accountId() ?? bot.account_id,
      'bots',
      bot.strategy_instance_id,
    ];
  }

  protected pnlTone(value: number | null): 'positive' | 'negative' | 'neutral' {
    if (value === null || value === 0) return 'neutral';
    return value > 0 ? 'positive' : 'negative';
  }

  protected onStart(bot: BotCatalogView): void {
    this.rowAction.emit({ bot, action: 'start' });
  }

  protected onStop(bot: BotCatalogView): void {
    this.rowAction.emit({ bot, action: 'stop' });
  }

  private supports(action: ActionId): boolean {
    return this.profile()?.supported_action_ids.includes(action) ?? false;
  }
}
