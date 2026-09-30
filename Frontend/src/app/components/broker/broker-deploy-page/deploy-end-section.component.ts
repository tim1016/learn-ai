import { ChangeDetectionStrategy, Component, input, model, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BotEndPopoverComponent } from '../bot-end/bot-end-popover.component';
import { BotEndRefusalComponent } from '../bot-end/bot-end-refusal.component';
import { BotEndSummaryComponent } from '../bot-end/bot-end-summary.component';
import type { BotEndFields } from '../bot-end/bot-end-fields';
import type { BotEndView } from '../v2-panel/lib/broker-v2-panel.service';
import type { ActionRejection } from '../v2-panel/lib/panel-action-outcome';

let nextEndSectionId = 0;

/**
 * How's end (#2607): when this bot stops, and whether it sells or keeps its
 * shares then.
 *
 * The column shows the minute the Deploy would send, in the viewer's own
 * time and market time, and the backend's words for exactly that end — its
 * headline and any move to one minute before an early close — or its
 * refusal. While the end is being checked, or when the check could not be
 * read (which holds nothing: the Deploy checks the end itself), it says so
 * plainly instead of showing an earlier end's words. The fields open over
 * the page, never in the column, so How never grows past one screen
 * (PR #2581): a date and a time in the viewer's zone, "No end", and Sell or
 * Keep (never Keep for a Dry Run, which always sells at its end).
 */
@Component({
  selector: 'app-deploy-end-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndPopoverComponent, BotEndRefusalComponent, BotEndSummaryComponent, ReceiptLabelPipe],
  templateUrl: './deploy-end-section.component.html',
  styleUrl: './deploy-end-section.component.scss',
})
export class DeployEndSectionComponent {
  readonly fields = model.required<BotEndFields>();
  readonly keepOffered = input(true);
  /** The minute the Deploy would send; `null` for no end, or while the fields name none. */
  readonly endAtMs = input<number | null>(null);
  /** The backend's words for exactly the end the Deploy would send, or `null` while none answer it. */
  readonly end = input<BotEndView | null>(null);
  /** The backend's refusal of exactly that end. */
  readonly refusal = input<ActionRejection | null>(null);
  /** That end is being checked. */
  readonly checking = input(false);
  /** Its check could not be read. */
  readonly unchecked = input(false);
  /** Offer "Use the default end": the owner chose their own, or the end was refused. */
  readonly offerDefault = input(false);

  readonly useDefault = output();

  private readonly id = nextEndSectionId++;
  protected readonly titleId = `deploy-end-${this.id}-title`;
}
