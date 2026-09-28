import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  type BoundAccountWorkspaceAddress,
} from '../../../fleet/account-workspace';
import type { BotCatalogView } from '../../broker/v2-panel/lib/broker-v2-panel.types';

/** What one bot's control is: Stop while it runs, Flatten… while it is
 * stopped still holding money, and nothing once it is done. */
type BotControl = 'stop' | 'flatten' | null;

/**
 * One bot's command on Home, the same on a List row and a Wall tile.
 *
 * Stop asks once, inline, and then hands the bot back to the page, which owns
 * the one action path and the outcome's focus. Flatten… opens the bot's page,
 * where the stopped-but-holding warning, the prepared plan and its
 * confirmation live: a flatten is prepared from fresh evidence, never fired
 * from a list.
 */
@Component({
  selector: 'app-home-bot-action',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './home-bot-action.component.html',
  styleUrl: './home-bot-action.component.scss',
  host: { '(keydown.escape)': 'cancel()' },
})
export class HomeBotActionComponent {
  readonly bot = input.required<BotCatalogView>();
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  /** The page's Stop for this bot is in flight. */
  readonly pending = input(false);
  readonly stopRequested = output<string>();

  protected readonly confirming = signal(false);
  private readonly cancelButton = viewChild<ElementRef<HTMLButtonElement>>('cancelButton');
  private readonly stopButton = viewChild<ElementRef<HTMLButtonElement>>('stopButton');

  protected readonly control = computed<BotControl>(() => {
    const bot = this.bot();
    if (bot.running) return 'stop';
    return bot.group === 'holding' ? 'flatten' : null;
  });

  protected readonly botLink = computed(
    () => accountWorkspaceBotRoute(this.account(), this.bot().strategy_instance_id).commands,
  );

  /** What Stop hands back, said before it happens. A Dry Run never used the
   * account's money, so it hands back nothing. */
  protected readonly stopConsequence = computed(() =>
    this.bot().group === 'dry_run'
      ? 'It makes no new decisions. A Dry Run never used this account’s money.'
      : 'It makes no new decisions. Its free budget returns to free to deploy; any shares it holds stay until you sell them.',
  );

  protected ask(): void {
    this.confirming.set(true);
    queueMicrotask(() => this.cancelButton()?.nativeElement.focus());
  }

  protected confirm(): void {
    this.confirming.set(false);
    this.stopRequested.emit(this.bot().strategy_instance_id);
  }

  cancel(): void {
    if (!this.confirming()) return;
    this.confirming.set(false);
    // The Cancel button leaves the DOM with the confirmation; hand the
    // keyboard back to the control that opened it.
    queueMicrotask(() => this.stopButton()?.nativeElement.focus());
  }
}
