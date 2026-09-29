import { ChangeDetectionStrategy, Component, computed, input, output, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  accountWorkspaceDeployAgainRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TypedHaltConfirmComponent } from '../../broker/shared/typed-halt-confirm/typed-halt-confirm.component';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';
import { HOME_CLEAR_COPY, MAX_CLEAR_BOTS, type ClearOutcome } from './home-clear';
import { HomeClearOutcomeComponent } from './home-clear-outcome.component';

/**
 * Home's Finished fold (PRD #2560 D7): bots that are stopped, flat and fully
 * released — Dry Runs included, marked as simulated cash — which move here by
 * themselves. Each shows when it ended, its trades, its whole-life result
 * (Python-authored, or "unknown" when the fee evidence cannot vouch for it —
 * never $0) and Deploy again. Folded by default.
 *
 * The owner clears them from here and only here (owner decision 2026-09-28):
 * tick bots, or clear them all, behind one plain confirmation. The fold owns
 * the selection and the confirmation; Home sends the clear, fenced to the lane
 * it was shown, and hands the outcome back.
 */
@Component({
  selector: 'app-home-finished',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, HomeClearOutcomeComponent, RouterLink, TimestampDisplayComponent, TypedHaltConfirmComponent],
  templateUrl: './home-finished.component.html',
  styleUrl: './home-finished.component.scss',
})
export class HomeFinishedComponent {
  readonly bots = input.required<readonly BotCatalogView[]>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  /** How many bots a clear in flight carries, or `null` when none is. */
  readonly clearing = input<number | null>(null);
  /** What the last clear did, until the owner dismisses it. */
  readonly clearOutcome = input<ClearOutcome | null>(null);

  /** The confirmed bots, in the order they are listed. */
  readonly clearRequested = output<readonly string[]>();
  readonly retryRequested = output();
  readonly outcomeDismissed = output();

  protected readonly copy = HOME_CLEAR_COPY;
  private readonly outcomePanel = viewChild(HomeClearOutcomeComponent);

  protected readonly rows = computed(() =>
    this.bots().map((bot) => ({
      bot,
      dryRun: bot.mode === 'dry_run',
      page: accountWorkspaceBotRoute(this.account(), bot.strategy_instance_id).commands,
      again: accountWorkspaceDeployAgainRoute(this.account(), bot.strategy_instance_id),
      againLabel: `Deploy again from ${bot.strategy_instance_id}`,
      selectLabel: HOME_CLEAR_COPY.select(bot.strategy_instance_id),
    })),
  );

  private readonly ticked = signal<ReadonlySet<string>>(new Set());
  /** Derived from the list, so a tick never outlives the bot it named. */
  protected readonly selected = computed(() =>
    this.bots().map((bot) => bot.strategy_instance_id).filter((sid) => this.ticked().has(sid)),
  );
  protected readonly allSelected = computed(() => this.bots().length > 0 && this.selected().length === this.bots().length);
  protected readonly someSelected = computed(() => this.selected().length > 0 && !this.allSelected());

  /** The bots the open confirmation names, frozen when it opened. */
  protected readonly pending = signal<readonly string[] | null>(null);

  protected readonly overCap = computed(
    () => this.bots().length > MAX_CLEAR_BOTS || this.selected().length > MAX_CLEAR_BOTS,
  );

  protected canClear(count: number): boolean {
    return count > 0 && count <= MAX_CLEAR_BOTS && this.clearing() === null;
  }

  protected isSelected(sid: string): boolean {
    return this.ticked().has(sid);
  }

  protected toggle(sid: string): void {
    this.ticked.update((current) => {
      const next = new Set(current);
      if (!next.delete(sid)) next.add(sid);
      return next;
    });
  }

  protected toggleAll(): void {
    this.ticked.set(this.allSelected() ? new Set() : new Set(this.bots().map((bot) => bot.strategy_instance_id)));
  }

  protected review(sids: readonly string[]): void {
    if (this.canClear(sids.length)) this.pending.set(sids);
  }

  protected reviewAll(): void {
    this.review(this.bots().map((bot) => bot.strategy_instance_id));
  }

  protected confirm(): void {
    const sids = this.pending();
    this.pending.set(null);
    if (sids === null) return;
    this.ticked.set(new Set());
    this.clearRequested.emit(sids);
  }

  /** Move the keyboard to what the last clear did. */
  focusOutcome(): void {
    this.outcomePanel()?.focus();
  }
}
