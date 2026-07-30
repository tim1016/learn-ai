import { Injectable, signal } from '@angular/core';
import type { MarkdownDocId } from './markdown-drawer.model';

/**
 * App-wide state for the single shell-level markdown drawer host.
 *
 * The drawer is mounted once at `app-root`; any component can call
 * `open(docId, anchor?)` to show a registered document at a specific section
 * without prop-drilling through the component tree.
 *
 * Only one document can be visible at a time — `open()` closes any
 * previously-open document before switching to the new one.
 *
 * Canonical consumers:
 *   - Methodology context help  → docId `'methodology'`
 *   - Broker-v2 card help       → docId `'broker-v2-manual'`  (via `BrokerV2CardHelpButtonComponent`)
 *
 * S4 wiring note: `BrokerV2CardHelpButtonComponent` injects this service
 * directly.  S4 components call `open('broker-v2-manual', anchor)` with a
 * `BrokerV2CardName`-derived anchor from `BROKER_V2_CARD_ANCHOR_MAP`.
 */
@Injectable({ providedIn: 'root' })
export class MarkdownDrawerService {
  /** The document currently loaded in the drawer, or `null` when closed. */
  readonly activeDocId = signal<MarkdownDocId | null>(null);

  /**
   * Anchor slug to scroll to (without the leading `#`).  Changes every time
   * the drawer opens so the viewer re-scrolls even if the same anchor is
   * opened twice in a row (e.g. after the user has scrolled away).
   */
  readonly anchor = signal<string | null>(null);

  /** Monotonic counter — increments each `open()` to bust effect caches. */
  readonly openTick = signal<number>(0);

  /** Whether any drawer is currently visible. */
  readonly visible = signal<boolean>(false);

  open(docId: MarkdownDocId, anchor?: string): void {
    this.activeDocId.set(docId);
    this.anchor.set(anchor ?? null);
    this.visible.set(true);
    this.openTick.update((n: number) => n + 1);
  }

  close(): void {
    this.visible.set(false);
  }
}
