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
 * the backend's words. The page holds Change and Save still (`locked`)
 * while another command on this bot is on its way, so a change of end never
 * races a Stop. The fields are read from the end once, as they open, so a
 * poll never rewrites what the owner is typing.
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

  protected readonly titleId = `bot-end-${nextEndCardId++}-title`;
  protected readonly keepOffered = computed(() => !this.dryRun());
  protected readonly fields = signal<BotEndFields>({ date: '', clock: '', noEnd: true, action: 'SELL' });
  protected readonly saving = signal(false);
  protected readonly busy = computed(() => this.saving() || this.locked());
  protected readonly refusal = signal<ActionRejection | null>(null);

  private readonly editor = viewChild.required(BotEndPopoverComponent);

  /** Change reads the fields from the end on screen, before they open. */
  protected prepare(): void {
    this.fields.set(botEndFields(this.end()));
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
      this.editor().hide();
    } catch (error) {
      this.refusal.set(deriveActionRejection(error, 'This bot’s end could not be changed.'));
    } finally {
      this.saving.set(false);
    }
  }
}
