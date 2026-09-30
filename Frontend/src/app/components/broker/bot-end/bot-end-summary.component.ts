import { ChangeDetectionStrategy, Component, booleanAttribute, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { BotEndView } from '../v2-panel/lib/broker-v2-panel.service';

/**
 * A bot's end as the owner reads it (#2607), drawn one way on Deploy and on
 * the bot's page: the backend's headline, the minute in the viewer's own
 * time and in market time, what happens then (`detailed`), and any move
 * before an early close.
 *
 * `end` is the backend's words, or `null` while none answer the end on
 * screen; `endAtMs` is the minute to show until they do, which a host may
 * take from the end it sends, so the times never wait on the words. Once
 * words answer, their own minute is shown — an end moved before an early
 * close is said at the minute it will be, never the one typed. Its lines
 * join the host's own layout (`display: contents`).
 */
@Component({
  selector: 'app-bot-end-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  template: `
    @let shown = end();
    @let at = shown !== null ? shown.end_at_ms : endAtMs();
    @if (shown !== null) { <p class="bot-end-summary__headline">{{ shown.headline }}</p> }
    @if (at !== null) {
      <p class="bot-end-summary__time">
        <app-timestamp-display [value]="at" mode="local" granularity="time" /> your time ·
        <app-timestamp-display [value]="at" mode="et" granularity="time" />
      </p>
    }
    @if (shown !== null && detailed()) { <p class="bot-end-summary__explanation">{{ shown.explanation }}</p> }
    @if (shown?.notice; as notice) { <p class="bot-end-summary__notice">{{ notice }}</p> }
  `,
  styles: `
    :host { display: contents; }

    p { margin: 0; line-height: 1.45; }

    .bot-end-summary__headline { color: var(--text-primary); font-size: var(--fs-sm); font-weight: var(--fw-semi); }

    .bot-end-summary__time,
    .bot-end-summary__explanation { color: var(--text-secondary); font-size: var(--fs-xs); }

    .bot-end-summary__notice { color: var(--warn); font-size: var(--fs-xs); }
  `,
})
export class BotEndSummaryComponent {
  readonly end = input<BotEndView | null>(null);
  readonly endAtMs = input<number | null>(null);
  /** Also say what happens at the end. */
  readonly detailed = input(false, { transform: booleanAttribute });
}
