import { Injectable, signal } from '@angular/core';

/**
 * App-wide state for the broker-v2 help drawer. The drawer is mounted once
 * at the app-shell level; any component can call `open(anchor)` to show the
 * operator manual at a specific section without prop-drilling through the
 * component tree.
 *
 * Canonical pattern: `shared/methodology-drawer/methodology-drawer.service.ts`.
 * Duplication is intentional: the two drawers serve different documents and
 * are mounted at the same shell level. A shared generic service would need to
 * carry per-drawer identity state (doc URL, title, etc.) and route calls to
 * the right visible signal — adding more coupling than it removes. When a
 * third markdown drawer is needed, extract a generic `MarkdownDrawerService`
 * at that time (see CLAUDE.md §5).
 */
@Injectable({ providedIn: 'root' })
export class BrokerV2HelpDrawerService {
  /** Whether the drawer is currently visible. */
  readonly visible = signal<boolean>(false);

  /**
   * Anchor slug to scroll to (without the leading `#`). Changes every time
   * the drawer opens so the viewer re-scrolls even if the same anchor is
   * opened twice in a row (e.g. after the user has scrolled away).
   */
  readonly anchor = signal<string | null>(null);

  /** Monotonic counter — increments each `open()` to bust effect caches. */
  readonly openTick = signal<number>(0);

  open(anchor?: string): void {
    if (anchor) this.anchor.set(anchor);
    this.visible.set(true);
    this.openTick.update((n: number) => n + 1);
  }

  close(): void {
    this.visible.set(false);
  }
}
