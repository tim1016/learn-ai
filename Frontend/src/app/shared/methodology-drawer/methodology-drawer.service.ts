import { Injectable, signal } from '@angular/core';

/**
 * App-wide state for the methodology drawer. The drawer is mounted once at
 * the app shell level; any component can call `open(anchor)` to show the
 * doc at a specific section without prop-drilling through the component
 * tree.
 */
@Injectable({ providedIn: 'root' })
export class MethodologyDrawerService {
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
    this.openTick.update(n => n + 1);
  }

  close(): void {
    this.visible.set(false);
  }
}
