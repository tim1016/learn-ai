import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotPanelView } from '../lib/broker-v2-panel.types';

/**
 * The narrative half of the triage pane: what the market did, and what the bot
 * decided about it.
 *
 * The design puts a live chart in the market slot. The real chart lives on the
 * per-bot panel route, which already owns the SSE stream and the history
 * fetches; this renders the same context from `market_pulse` — already in the
 * panel payload — and links across rather than standing up a second stream.
 */
@Component({
  selector: 'app-triage-activity',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './triage-activity.component.html',
  host: { class: 'triage-column' },
})
export class TriageActivityComponent {
  readonly view = input.required<BotPanelView>();
  readonly botLink = input.required<readonly string[]>();
}
