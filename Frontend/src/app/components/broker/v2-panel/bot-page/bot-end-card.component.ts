import { ChangeDetectionStrategy, Component, computed, input, signal, viewChild } from '@angular/core';

import { BotEndPopoverComponent } from '../../bot-end/bot-end-popover.component';
import { BotEndRefusalComponent } from '../../bot-end/bot-end-refusal.component';
import { BotEndSummaryComponent } from '../../bot-end/bot-end-summary.component';
import { botEndFields, botEndInput, type BotEndFields } from '../../bot-end/bot-end-fields';
import type { BotEndInput, BotEndView } from '../lib/broker-v2-panel.service';
import { deriveActionRejection, type ActionRejection } from '../lib/panel-action-outcome';

let nextEndCardId = 0;

/**
 * A bot's end on its page (#2607): the backend's headline, what will happen
 * or what did, the same minute in the viewer's time and market time, and
 * any move before an early close — every word as the panel poll carries it.
 *
 * While the backend says the end is `editable`, Change opens the fields over
 * the page: a date and a time in the viewer's zone, "No end", and Sell or
 * Keep (never Keep for a Dry Run). Save sends the choice through `save` —
 * the page's own fenced, one-at-a-time command — and the next panel read
 * shows the new end; a refusal (400) or a state conflict (409) is shown in
 * the backend's words — in the fields while they are open, and on the card
 * once they are closed, so a refusal that lands after the owner closed them
 * mid-save is still seen until Change opens them again. The page holds
 * Change and Save still (`locked`) while another command on this bot is on
 * its way, so a change of end never races a Stop. The fields are read from
 * the end once, as they open, so a poll never rewrites what the owner is
 * typing: the bot's own end, or — for a bot with none — the default end the
 * backend offers (`default_end_at_ms`), as Deploy's fields start (#2663).
 */
@Component({
  selector: 'app-bot-end-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndPopoverComponent, BotEndRefusalComponent, BotEndSummaryComponent],
  templateUrl: './bot-end-card.component.html',
  styleUrl: './bot-end-card.component.scss',
})
export class BotEndCardComponent {
  readonly end = input.required<BotEndView>();
  readonly dryRun = input(false);
  /** Another command on this bot is on its way. */
  readonly locked = input(false);
  /** Sends the owner's choice; resolves once it is recorded, rejects with the refusal. */
  readonly save = input.required<(choice: BotEndInput) => Promise<void>>();
  /** The bot page's toolbar owns Change end (#2794), so the card there shows no button of its own. */
  readonly showChange = input(true);

  protected readonly titleId = `bot-end-${nextEndCardId++}-title`;
  protected readonly keepOffered = computed(() => !this.dryRun());
  protected readonly fields = signal<BotEndFields>({ date: '', clock: '', noEnd: true, action: 'SELL' });
  protected readonly saving = signal(false);
  protected readonly busy = computed(() => this.saving() || this.locked());
  protected readonly refusal = signal<ActionRejection | null>(null);

  private readonly editor = viewChild.required(BotEndPopoverComponent);

  /** Change reads the fields from the end on screen, before they open: its
   * own end, or the default end offered to a bot with none. */
  /** Open the end's fields, as the card's own Change does; nothing while a save or command is on its way. */
  openEditor(): void {
    if (this.busy() || !this.end().editable) return;
    this.prepare();
    this.editor().show();
  }

  protected prepare(): void {
    const end = this.end();
    this.fields.set(botEndFields({ end_at_ms: end.end_at_ms ?? end.default_end_at_ms, end_action: end.end_action }));
    this.refusal.set(null);
  }

  protected async submit(): Promise<void> {
    if (this.busy()) return;
    const choice = botEndInput(this.fields(), this.keepOffered());
    if (choice === null) {
      this.editor().revealErrors();
      return;
    }
    this.saving.set(true);
    this.refusal.set(null);
    try {
      await this.save()(choice);
    } catch (error) {
      this.refusal.set(deriveActionRejection(error, 'This bot’s end could not be changed.'));
      return;
    } finally {
      this.saving.set(false);
    }
    this.editor().hide();
  }
}
