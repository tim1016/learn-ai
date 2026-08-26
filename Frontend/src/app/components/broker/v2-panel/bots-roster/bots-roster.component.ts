import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtExposure, fmtInteger, fmtSignedCurrency } from '../../format';
import { actionTone } from '../bot-detail-banner/lifecycle-action';
import type {
  BotCatalogView,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

/** One roster row's recovery command, named with the row it belongs to. */
export interface RosterRowActionEvent {
  readonly bot: BotCatalogView;
  readonly action: PanelAction;
}

/** Rail grouping. `retired` has no design chip; it appears only when populated. */
type RailGroup = 'attention' | 'running' | 'stopped' | 'retired';

interface GroupDefinition {
  readonly key: RailGroup;
  readonly label: string;
}

/**
 * Right-aligned rail value. Timestamps stay `int64 ms UTC` all the way to the
 * shared display component (temporal-rigor.md); only non-temporal values are
 * pre-formatted strings.
 */
type RailValue =
  | { readonly kind: 'text'; readonly text: string }
  | { readonly kind: 'time'; readonly atMs: number };

interface RailBotRow {
  readonly bot: BotCatalogView;
  /** Right-aligned value: P&L, exposure, or last activity depending on group. */
  readonly value: RailValue;
  readonly valueTone: 'positive' | 'negative' | 'neutral' | 'muted' | 'alert';
  /**
   * The backend-authored recovery command for this row, or null. Offered
   * only on attention rows: a healthy row is not a place to invite a
   * recovery mutation.
   */
  readonly action: PanelAction | null;
  /** Second line: backend `status_label` plus a compact exposure/fills fact. */
  readonly detail: string;
  readonly dotTone: 'bear' | 'warn' | 'bull' | 'info' | 'muted';
  /**
   * Explicit accessible name. The severity dot is decorative, so attention
   * state has to reach assistive tech as words, not just colour.
   */
  readonly ariaLabel: string;
}

interface RailGroupView {
  readonly key: RailGroup;
  readonly label: string;
  readonly rows: readonly RailBotRow[];
}

const GROUPS: readonly GroupDefinition[] = [
  { key: 'attention', label: 'Needs attention' },
  { key: 'running', label: 'Running' },
  { key: 'stopped', label: 'Stopped' },
  { key: 'retired', label: 'Retired' },
];

/**
 * Triage rail — the fleet roster as a scannable 320px column.
 *
 * Grouped attention-first. Each row carries the backend-authored
 * `status_label` plus facts derived only from `BotCatalogView` fields; the
 * full `status_explanation` prose belongs to the detail pane, which has the
 * width for it.
 *
 * `retired` has no chip in the design but is still grouped and rendered when
 * populated — dropping the group would silently hide fleet members.
 */
@Component({
  selector: 'app-bots-roster',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PanelActionButtonComponent, TimestampDisplayComponent],
  templateUrl: './bots-roster.component.html',
  styleUrl: './bots-roster.component.scss',
  host: { class: 'flex min-h-0 flex-1 flex-col' },
})
export class BotsRosterComponent {
  readonly bots = input.required<BotCatalogView[]>();
  readonly selectedSid = input<string | null>(null);
  readonly updatedAtMs = input<number | null>(null);
  readonly botSelected = output<string>();
  readonly rowActionRequested = output<RosterRowActionEvent>();

  protected readonly searchTerm = signal('');
  protected readonly groupFilter = signal<RailGroup | null>(null);

  /**
   * The canonical action-id → tone map (`lifecycle-action.ts`), reused rather
   * than re-decided here. Hardcoding `danger` painted whatever the backend
   * picked as primary in destructive red — commonly `reconcile_now` (a
   * refresh) or the read-only `open_custody_timeline` — which teaches an
   * operator that red means nothing.
   */
  protected readonly actionTone = actionTone;

  private readonly grouped = computed(() => {
    const buckets = new Map<RailGroup, BotCatalogView[]>(GROUPS.map((g) => [g.key, []]));
    for (const bot of this.bots()) {
      buckets.get(this.groupOf(bot))?.push(bot);
    }
    return buckets;
  });

  /** Chip counts stay whole-fleet: they are the map, not a view of the filter. */
  protected readonly counts = computed(() => {
    const buckets = this.grouped();
    return GROUPS.map((group) => ({
      key: group.key,
      label: group.label,
      count: buckets.get(group.key)?.length ?? 0,
    })).filter((entry) => entry.key !== 'retired' || entry.count > 0);
  });

  protected readonly groups = computed<readonly RailGroupView[]>(() => {
    const term = this.searchTerm().toLowerCase().trim();
    const filter = this.groupFilter();
    const buckets = this.grouped();

    return GROUPS.filter((group) => filter === null || filter === group.key)
      .map((group) => ({
        key: group.key,
        label: group.label,
        rows: (buckets.get(group.key) ?? [])
          .filter((bot) => this.matchesTerm(bot, term))
          .sort((left, right) => (right.last_activity_at_ms ?? 0) - (left.last_activity_at_ms ?? 0))
          .map((bot) => this.toRow(bot)),
      }))
      .filter((group) => group.rows.length > 0);
  });

  protected readonly visibleCount = computed(() =>
    this.groups().reduce((total, group) => total + group.rows.length, 0),
  );

  protected readonly emptyMessage = computed(() =>
    this.bots().length === 0
      ? 'No Alpaca bots yet. Deploy a strategy to create the first fleet member.'
      : 'No bots match this filter. Clear the search or choose another group.',
  );

  protected onSearch(event: Event): void {
    const target = event.target;
    this.searchTerm.set(target instanceof HTMLInputElement ? target.value : '');
  }

  /** Clicking the active chip clears the filter, so "all" needs no chip of its own. */
  protected toggleGroup(group: RailGroup): void {
    this.groupFilter.update((current) => (current === group ? null : group));
  }

  protected select(bot: BotCatalogView): void {
    this.botSelected.emit(bot.strategy_instance_id);
  }

  protected requestRowAction(
    bot: BotCatalogView,
    { action }: PanelActionTrigger,
  ): void {
    this.rowActionRequested.emit({ bot, action });
  }

  private matchesTerm(bot: BotCatalogView, term: string): boolean {
    if (!term) return true;
    return (
      bot.strategy_instance_id.toLowerCase().includes(term) ||
      bot.symbol.toLowerCase().includes(term) ||
      bot.strategy_key.toLowerCase().includes(term) ||
      bot.strategy_label.toLowerCase().includes(term)
    );
  }

  private groupOf(bot: BotCatalogView): RailGroup {
    if (bot.phase === 'RETIRED') return 'retired';
    if (bot.needs_attention) return 'attention';
    return bot.running ? 'running' : 'stopped';
  }

  private toRow(bot: BotCatalogView): RailBotRow {
    const group = this.groupOf(bot);
    const exposure = fmtExposure(bot.exposure);
    const detail = this.detailText(bot, exposure);
    // Attention is a property of the bot, never of the group it sorts into.
    // `groupOf` tests RETIRED first, so a retired bot still holding stranded
    // exposure lands outside the `attention` group — and reading its severity
    // off the group is what muted it and dropped its authored cure (#1778).
    const attention = bot.needs_attention;
    return {
      bot,
      ...this.railValue(bot, group, attention, exposure),
      detail,
      // Pass-through, not a re-gate. The backend decides which rows carry a
      // recovery command (`_catalog_row_action`); a second frontend gate is
      // precisely how the authored cure went missing.
      action: bot.row_action ?? null,
      dotTone: this.dotTone(bot, group, attention),
      ariaLabel: attention
        ? `${bot.strategy_instance_id}, needs attention: ${detail}`
        : `${bot.strategy_instance_id}, ${detail}`,
    };
  }

  private railValue(
    bot: BotCatalogView,
    group: RailGroup,
    attention: boolean,
    exposure: string,
  ): { value: RailValue; valueTone: RailBotRow['valueTone'] } {
    // An attention row's state is the point of the row; muting it is what
    // let a crashed bot read like a deliberately stopped one (#1778).
    if (attention) {
      return { value: this.lastActivity(bot), valueTone: 'alert' };
    }
    if (group === 'retired') {
      return { value: this.lastActivity(bot), valueTone: 'muted' };
    }
    if (bot.mode === 'dry_run') {
      return { value: { kind: 'text', text: 'Dry run' }, valueTone: 'muted' };
    }
    if (group === 'stopped') {
      return {
        value: { kind: 'text', text: exposure },
        valueTone: exposure === 'Flat' ? 'muted' : 'neutral',
      };
    }
    const total = bot.day_pnl;
    if (total === null) {
      return { value: this.lastActivity(bot), valueTone: 'muted' };
    }
    return {
      value: { kind: 'text', text: fmtSignedCurrency(total) },
      valueTone: total > 0 ? 'positive' : total < 0 ? 'negative' : 'neutral',
    };
  }

  /**
   * `status_label` is the backend's closed vocabulary; the trailing facts come
   * only from catalog fields. No gate id is invented — `BotCatalogView` has no
   * such field, and the detail pane names the gate from the panel view.
   */
  private detailText(bot: BotCatalogView, exposure: string): string {
    const facts: string[] = [exposure];
    const fills = bot.fills_today;
    if (fills !== null && fills > 0) {
      facts.push(`${fmtInteger(fills)} ${fills === 1 ? 'fill' : 'fills'}`);
    }
    return `${bot.status_label} · ${facts.join(' · ')}`;
  }

  private dotTone(
    bot: BotCatalogView,
    group: RailGroup,
    attention: boolean,
  ): RailBotRow['dotTone'] {
    if (attention) return bot.phase === 'OFF_DUTY' ? 'bear' : 'warn';
    if (group === 'retired' || group === 'stopped') return 'muted';
    return bot.mode === 'dry_run' ? 'info' : 'bull';
  }

  private lastActivity(bot: BotCatalogView): RailValue {
    const atMs = bot.last_activity_at_ms;
    return atMs === null ? { kind: 'text', text: 'No activity' } : { kind: 'time', atMs };
  }
}
