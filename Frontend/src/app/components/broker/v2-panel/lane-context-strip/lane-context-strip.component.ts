import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LaneDescriptor } from '../../../../fleet/fleet-directory.types';
import { AlpacaLiveBannerComponent } from '../../../../shell/alpaca-live-banner.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';

/**
 * The routed lane's context strip: its own trust-anchor pill (the same pill
 * the shell renders) plus its authority fact, so a bot-scoped page never
 * loses sight of which lane/account it serves. Extracted from two
 * byte-identical inline blocks in `bots-list-page` and `bot-gallery-page`
 * (#2168) — this is the canonical implementation of that markup.
 *
 * Takes the already-resolved `LaneDescriptor` directly: the caller already
 * computes its own `routedLane` from `FleetDirectoryService` (for its own
 * frozen-fence and target-scoping needs) and passes that value straight in.
 * This component does not re-derive the lane from `broker`/`clerkId` inputs
 * and has no need to inject `FleetDirectoryService` itself — unlike its own
 * child `AlpacaLiveBannerComponent` (and, elsewhere, `AlpacaLaneCardComponent`),
 * which now injects `FleetDirectoryService` directly to look up sibling lanes
 * for disambiguation. That is a leaf-component concern neither this strip nor
 * its callers share, not a convention this strip is bound to follow.
 *
 * Deliberately does not own the page's wrapping container — each caller's
 * `.bots-page__lane` / `.gallery-page__lane` flex/gap/padding differ and
 * stay in the page — nor any page-specific sibling markup, such as the
 * gallery's "Bots roster" back-link. Only the shared pill+authority fact.
 */
@Component({
  selector: 'app-lane-context-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaLiveBannerComponent, ReceiptLabelPipe],
  templateUrl: './lane-context-strip.component.html',
  styleUrl: './lane-context-strip.component.scss',
})
export class LaneContextStripComponent {
  /** The routed lane, or `null` when none has resolved yet — renders nothing
   * rather than an empty pill. */
  readonly lane = input.required<LaneDescriptor | null>();
}
