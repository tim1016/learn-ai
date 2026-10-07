import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { OptionContractComponent } from '../../../../shared/option-contract/option-contract.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { OptionStructurePreview } from '../../v2-panel/lib/broker-v2-panel.service';
import { ageSeconds, deltaText, quoteAgeSeconds } from './option-preview-display';
import { OptionPreviewFiguresComponent } from './option-preview-figures.component';
import { sideInWords } from './option-structure';

let nextTitleId = 0;

/**
 * What a structure picks right now (#2915): the preview's answer, as the
 * server sends it.
 *
 * `state` is a closed set, and only `resolved` carries figures. Every other
 * state shows the server's own sentence for it and nothing else, so a state
 * this component has never heard of still reads correctly. A resolved answer
 * shows each picked contract through the shared contract renderer, with the
 * time its quotes were read. An answer is as old as that time says: it is
 * asked again when the legs change or the owner refreshes. A state that
 * passes by itself is asked again for a while without the owner; once that
 * stops, the panel says so beside Refresh, in its own words.
 *
 * `compact` is Deploy's short view of the same answer.
 */
@Component({
  selector: 'app-option-preview-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OptionContractComponent, OptionPreviewFiguresComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './option-preview-panel.component.html',
  styleUrl: './option-preview-panel.component.scss',
})
export class OptionPreviewPanelComponent {
  readonly heading = input.required<string>();
  /** The answer to what is on screen; `null` while there is none. */
  readonly preview = input<OptionStructurePreview | null>(null);
  readonly asking = input(false);
  /** Why the ask got no answer at all, when it did not. */
  readonly failure = input<string | null>(null);
  /** The answer says to ask again and the page no longer does so by itself: only Refresh asks. */
  readonly stopped = input(false);
  readonly compact = input(false);

  readonly refresh = output();

  private readonly id = nextTitleId++;
  protected readonly titleId = `option-preview-title-${this.id}`;
  protected readonly stoppedId = `option-preview-stopped-${this.id}`;

  /** The answer, when it is one that carries figures. */
  protected readonly resolved = computed(() => {
    const told = this.preview();
    return told?.state === 'resolved' ? told : null;
  });

  protected readonly oldestAge = computed(() => {
    const age = this.resolved()?.oldest_quote_age_ms ?? null;
    return age === null ? null : ageSeconds(age);
  });

  protected readonly legs = computed(() =>
    (this.resolved()?.legs ?? []).map((leg) => ({
      leg,
      side: sideInWords(leg.position),
      age: quoteAgeSeconds(leg.quote),
      ours: deltaText(leg.delta),
      ibkr: deltaText(leg.ibkr_delta),
    })),
  );
}
