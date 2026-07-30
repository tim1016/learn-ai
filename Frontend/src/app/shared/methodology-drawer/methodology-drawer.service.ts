import { Injectable, computed } from '@angular/core';
import { inject } from '@angular/core';
import { MarkdownDrawerService } from '../markdown-drawer/markdown-drawer.service';

/**
 * Façade for opening the indicator-reliability methodology document in the
 * single shell-level markdown drawer host.
 *
 * Preserved as a named service so existing consumers (`InfoIconComponent`,
 * `IndicatorReliabilityComponent`) need no import changes.
 *
 * Canonical implementation: `shared/markdown-drawer/markdown-drawer.service.ts`.
 * This service is a thin pass-through to `MarkdownDrawerService` with
 * `docId = 'methodology'` hardcoded.
 */
@Injectable({ providedIn: 'root' })
export class MethodologyDrawerService {
  private readonly drawer = inject(MarkdownDrawerService);

  /** Whether the methodology drawer is currently visible. */
  readonly visible = computed(() => this.drawer.visible() && this.drawer.activeDocId() === 'methodology');

  /** Current anchor slug, or null. */
  readonly anchor = computed(() =>
    this.drawer.activeDocId() === 'methodology' ? this.drawer.anchor() : null,
  );

  /** Monotonic open tick (forwarded from the generic service). */
  readonly openTick = computed(() =>
    this.drawer.activeDocId() === 'methodology' ? this.drawer.openTick() : 0,
  );

  open(anchor?: string): void {
    this.drawer.open('methodology', anchor);
  }

  close(): void {
    this.drawer.close();
  }
}
