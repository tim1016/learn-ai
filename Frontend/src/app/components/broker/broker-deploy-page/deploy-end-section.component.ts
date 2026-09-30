import { ChangeDetectionStrategy, Component, input, model, output, viewChild } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BotEndEditorComponent } from '../bot-end/bot-end-editor.component';
import type { BotEndFields, BotEndRefusal } from '../bot-end/bot-end-fields';
import type { BotEndView } from '../v2-panel/lib/broker-v2-panel.service';

let nextEndSectionId = 0;

/**
 * How's end (#2607): when this bot stops, and whether it sells or keeps its
 * shares then.
 *
 * The column shows the backend's own words for the end the form would send —
 * its headline, the same instant in the viewer's own time, and any move to
 * one minute before an early close — or the backend's refusal. The fields
 * open over the page, never in the column, so How never grows past one
 * screen (PR #2581): a date and a time in the viewer's zone, "No end", and
 * Sell or Keep (never Keep for a Dry Run, which always sells at its end).
 */
@Component({
  selector: 'app-deploy-end-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndEditorComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './deploy-end-section.component.html',
  styleUrl: './deploy-end-section.component.scss',
})
export class DeployEndSectionComponent {
  readonly fields = model.required<BotEndFields>();
  readonly keepOffered = input(true);
  /** The backend's words for the end the form would send, or the default end's. */
  readonly end = input<BotEndView | null>(null);
  readonly refusal = input<BotEndRefusal | null>(null);
  /** The fields still follow the account's default end. */
  readonly followsDefault = input(true);

  readonly useDefault = output();

  private readonly id = nextEndSectionId++;
  protected readonly titleId = `deploy-end-${this.id}-title`;
  protected readonly editorId = `deploy-end-${this.id}-editor`;
  protected readonly editorTitleId = `deploy-end-${this.id}-editor-title`;

  private readonly editor = viewChild.required(BotEndEditorComponent);

  /** The fields take the keyboard as they open over the page. */
  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'open') this.editor().focus();
  }
}
