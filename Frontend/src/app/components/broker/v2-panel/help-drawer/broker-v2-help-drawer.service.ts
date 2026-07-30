import { Injectable, computed } from '@angular/core';
import { inject } from '@angular/core';
import { MarkdownDrawerService } from '../../../../shared/markdown-drawer/markdown-drawer.service';

/**
 * Façade for opening the broker-v2 operator manual in the single shell-level
 * markdown drawer host.
 *
 * Preserved as a named service so `BrokerV2CardHelpButtonComponent` and any
 * future S4 operator-lens card consumers can inject it without knowing the
 * generic drawer internals.
 *
 * Canonical implementation: `shared/markdown-drawer/markdown-drawer.service.ts`.
 * This service is a thin pass-through with `docId = 'broker-v2-manual'`
 * hardcoded.
 *
 * S4 wiring note: S4 operator-lens cards will call `open(anchor)` with an
 * anchor derived from `BROKER_V2_CARD_ANCHOR_MAP[cardName]`.  The anchor map
 * and `BrokerV2CardName` type live in `broker-v2-card-anchor-map.ts`.  The
 * typed `BrokerV2CardHelpButtonComponent` (Fix 4) already enforces this —
 * S4 only needs to wire the `cardName` input.
 */
@Injectable({ providedIn: 'root' })
export class BrokerV2HelpDrawerService {
  private readonly drawer = inject(MarkdownDrawerService);

  /** Whether the broker-v2 help drawer is currently visible. */
  readonly visible = computed(
    () => this.drawer.visible() && this.drawer.activeDocId() === 'broker-v2-manual',
  );

  /** Current anchor slug, or null. */
  readonly anchor = computed(() =>
    this.drawer.activeDocId() === 'broker-v2-manual' ? this.drawer.anchor() : null,
  );

  /** Monotonic open tick (forwarded from the generic service). */
  readonly openTick = computed(() =>
    this.drawer.activeDocId() === 'broker-v2-manual' ? this.drawer.openTick() : 0,
  );

  open(anchor?: string): void {
    this.drawer.open('broker-v2-manual', anchor);
  }

  close(): void {
    this.drawer.close();
  }
}
