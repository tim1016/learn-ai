import { ScrollingModule } from '@angular/cdk/scrolling';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { SelectButtonModule } from 'primeng/selectbutton';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtInteger, fmtSignedCurrency } from '../../format';
import { BotStatusChipComponent } from '../bot-status-chip/bot-status-chip.component';
import type { ActionId, BotCatalogView, PanelProfile } from '../lib/broker-v2-panel.types';

export interface RowActionEvent {
  bot: BotCatalogView;
  action: ActionId;
}

type StatusFilter = 'all' | 'working' | 'off_duty' | 'retired';

const ITEM_SIZE = 48; // px — one table row height

@Component({
  selector: 'app-bots-roster',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    ButtonModule,
    InputTextModule,
    SelectButtonModule,
    ScrollingModule,
    BotStatusChipComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './bots-roster.component.html',
  styleUrl: './bots-roster.component.scss',
  host: { class: 'block' },
})
export class BotsRosterComponent {
  readonly bots = input.required<BotCatalogView[]>();
  readonly profile = input<PanelProfile | null>(null);
  readonly broker = input.required<string>();
  readonly accountId = input<string | undefined>(undefined);
  readonly rowAction = output<RowActionEvent>();

  protected readonly searchTerm = signal('');
  protected readonly statusFilter = signal<StatusFilter>('all');

  protected readonly itemSize = ITEM_SIZE;
  protected readonly fmtSignedCurrency = fmtSignedCurrency;
  protected readonly fmtInteger = fmtInteger;

  readonly statusOptions = [
    { label: 'All', value: 'all' },
    { label: 'Working', value: 'working' },
    { label: 'Off duty', value: 'off_duty' },
    { label: 'Retired', value: 'retired' },
  ];

  private readonly router = inject(Router);

  protected readonly filtered = computed(() => {
    const term = this.searchTerm().toLowerCase().trim();
    const filter = this.statusFilter();
    const bots = this.bots();

    const passed = bots.filter((b) => {
      const matchesTerm =
        !term ||
        b.strategy_instance_id.toLowerCase().includes(term) ||
        b.symbol.toLowerCase().includes(term);

      const matchesFilter =
        filter === 'all' ||
        (filter === 'working' && b.phase === 'ON_DUTY' && b.running) ||
        (filter === 'off_duty' && b.phase === 'OFF_DUTY') ||
        (filter === 'retired' && b.phase === 'RETIRED');

      return matchesTerm && matchesFilter;
    });

    // Attention-first sort, then by last_activity_at_ms desc
    return passed.sort((a, b) => {
      if (a.needs_attention !== b.needs_attention) {
        return a.needs_attention ? -1 : 1;
      }
      const aMs = a.last_activity_at_ms ?? 0;
      const bMs = b.last_activity_at_ms ?? 0;
      return bMs - aMs;
    });
  });

  protected exposureText(bot: BotCatalogView): string {
    const entries = Object.entries(bot.exposure);
    if (entries.length === 0) return '—';
    return entries.map(([sym, qty]) => `${sym}: ${qty}`).join(', ');
  }

  protected canStart(bot: BotCatalogView): boolean {
    const ids = this.profile()?.supported_action_ids ?? [];
    return bot.phase === 'OFF_DUTY' && ids.includes('start');
  }

  protected canStop(bot: BotCatalogView): boolean {
    const ids = this.profile()?.supported_action_ids ?? [];
    return bot.phase === 'ON_DUTY' && ids.includes('stop');
  }

  protected onStart(bot: BotCatalogView): void {
    this.rowAction.emit({ bot, action: 'start' });
  }

  protected onStop(bot: BotCatalogView): void {
    this.rowAction.emit({ bot, action: 'stop' });
  }

  protected botTrackFn(_index: number, bot: BotCatalogView): string {
    return bot.strategy_instance_id;
  }

  protected navigateToBot(bot: BotCatalogView): void {
    const accountId = this.accountId() ?? bot.account_id;
    void this.router.navigate([
      '/brokers',
      this.broker(),
      'accounts',
      accountId,
      'bots',
      bot.strategy_instance_id,
    ]);
  }
}
