import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  signal,
  viewChild,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotEndEditorComponent } from '../../bot-end/bot-end-editor.component';
import {
  botEndFields,
  botEndInput,
  botEndRefusal,
  type BotEndFields,
  type BotEndRefusal,
} from '../../bot-end/bot-end-fields';
import type { BotEndInput, BotEndView } from '../lib/broker-v2-panel.service';

/**
 * A bot's end on its page (#2607): the backend's headline, what will happen
 * or what did, the same instant in the viewer's time and market time, and
 * any move before an early close — every word as the panel poll carries it.
 *
 * While the backend says the end is `editable`, Change opens the fields over
 * the page: a date and a time in the viewer's zone, "No end", and Sell or
 * Keep (never Keep for a Dry Run). Save sends the choice through `save` —
 * the page's own fenced command — and the next panel read shows the new end;
 * a refusal (400) or a state conflict (409) is shown in the backend's words.
 * The fields are read from the end once, as they open, so a poll never
 * rewrites what the owner is typing.
 */
@Component({
  selector: 'app-bot-end-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndEditorComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './bot-end-card.component.html',
  styleUrl: './bot-end-card.component.scss',
})
export class BotEndCardComponent {
  readonly end = input.required<BotEndView>();
  readonly dryRun = input(false);
  /** Sends the owner's choice; resolves once it is recorded, rejects with the refusal. */
  readonly save = input.required<(choice: BotEndInput) => Promise<void>>();

  protected readonly keepOffered = computed(() => !this.dryRun());
  protected readonly fields = signal<BotEndFields>({ date: '', time: '', noEnd: true, action: 'SELL' });
  protected readonly saving = signal(false);
  protected readonly refusal = signal<BotEndRefusal | null>(null);

  private readonly popover = viewChild.required<ElementRef<HTMLElement>>('popover');
  private readonly editor = viewChild.required(BotEndEditorComponent);

  /** Change reads the fields from the end on screen, before they open. */
  protected prepare(): void {
    this.fields.set(botEndFields(this.end()));
    this.refusal.set(null);
  }

  /** The fields take the keyboard as they open over the page. */
  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'open') this.editor().focus();
  }

  protected async submit(): Promise<void> {
    const choice = botEndInput(this.fields(), this.keepOffered());
    if (choice === null || this.saving()) return;
    this.saving.set(true);
    this.refusal.set(null);
    try {
      await this.save()(choice);
      const popover = this.popover().nativeElement;
      if ('hidePopover' in popover) popover.hidePopover();
    } catch (error) {
      this.refusal.set(botEndRefusal(error, 'This bot’s end could not be changed.'));
    } finally {
      this.saving.set(false);
    }
  }
}
