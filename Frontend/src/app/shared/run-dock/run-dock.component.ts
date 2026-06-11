import {
  Component,
  ChangeDetectionStrategy,
  computed,
  effect,
  inject,
  signal,
  viewChild,
  ElementRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
  RunLogEntry,
} from './run-dock-source';

/**
 * Bottom-docked run UI. Pinned to the viewport bottom, always present in
 * the host page, toggles between an expanded panel (320 px tall, structured
 * progress strip + scrolling event log) and a collapsed status strip
 * (~36 px tall, single-line "what's running").
 *
 * The dock is source-agnostic: it reads from `RUN_DOCK_SOURCE` (a
 * surface-specific service implementing `RunDockSource`) so the same dock
 * renders data-lab's fetch / bundle pipeline and engine-lab's job phase
 * stream without knowing the difference.
 *
 * Surface-specific HUD content (metric chips, etc.) is projected via the
 * `[runDockMetrics]` content slot.
 *
 * Default state on first visit is collapsed. The user's explicit expand /
 * collapse persists in localStorage under `RUN_DOCK_STORAGE_KEY` so the
 * choice sticks across navigation and reloads. Each surface should
 * provide a unique storage key.
 */
@Component({
  selector: 'app-run-dock',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './run-dock.component.html',
  styleUrls: ['./run-dock.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '[class.run-dock--expanded]': 'expanded()',
    '[class.run-dock--collapsed]': '!expanded()',
  },
})
export class RunDockComponent {
  readonly source = inject(RUN_DOCK_SOURCE);
  private readonly storageKey = inject(RUN_DOCK_STORAGE_KEY);

  /** Default to collapsed; restored from localStorage if the user has
   *  explicitly toggled this surface's dock before. */
  readonly expanded = signal<boolean>(this._initialExpanded());

  private readonly logScroll = viewChild<ElementRef<HTMLDivElement>>('logScroll');

  /** Stay-at-bottom flag for the log auto-scroll. Flipped to false when
   *  the user scrolls up past the threshold; flipped back to true when
   *  they scroll near the bottom again. */
  private stickToBottom = true;
  private static readonly STICK_THRESHOLD_PX = 24;

  // Re-exported source signals so the template doesn't have to chain
  // through `source.` everywhere.
  readonly dockState = this.source.dockState;
  readonly headline = this.source.headline;
  readonly headlineLevel = this.source.headlineLevel;
  readonly progressPercent = this.source.progressPercent;
  readonly etaText = this.source.etaText;
  readonly canCancel = this.source.canCancel;
  readonly log = this.source.log;

  readonly hasDeterminateProgress = computed<boolean>(() => {
    return this.dockState() === 'active' && this.progressPercent() !== null;
  });

  toggle(): void {
    this.expanded.update((v) => {
      const next = !v;
      this._persistExpanded(next);
      return next;
    });
  }

  expand(): void {
    this.expanded.set(true);
    this._persistExpanded(true);
  }

  collapse(): void {
    this.expanded.set(false);
    this._persistExpanded(false);
  }

  clearLog(): void {
    this.source.clearLog();
  }

  cancel(): void {
    void this.source.cancel();
  }

  /** Pretty time stamp for log lines: HH:mm:ss.ms (24h, local). */
  formatTime(ms: number): string {
    const d = new Date(ms);
    const pad = (n: number, w = 2) => n.toString().padStart(w, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  /** Track-by for `@for` so DOM nodes are stable across appends. */
  trackById(_: number, entry: RunLogEntry): string {
    return entry.id;
  }

  constructor() {
    // Auto-scroll the log to the bottom whenever a new entry lands,
    // unless the user has scrolled up to read history.
    effect(() => {
      // Touch the log so this effect re-runs on every append.
      this.log();
      const el = this.logScroll()?.nativeElement;
      if (!el) return;
      if (!this.stickToBottom) return;
      // Defer so the DOM has time to commit the appended <li> nodes.
      queueMicrotask(() => {
        el.scrollTop = el.scrollHeight;
      });
    });

    // Publish the dock's current height to a CSS variable on :root so
    // the host page can reserve bottom padding equal to whatever the
    // dock is occupying right now (expanded 320 px vs collapsed 36 px).
    // Otherwise the last form controls hide under the dock.
    effect(() => {
      const height = this.expanded() ? '320px' : '36px';
      document.documentElement.style.setProperty('--run-dock-height', height);
    });
  }

  /** Scroll listener (bound in the template) so we know whether to keep
   *  auto-sticking to the bottom or pause for the user. */
  onLogScroll(): void {
    const el = this.logScroll()?.nativeElement;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - (el.scrollTop + el.clientHeight);
    this.stickToBottom = distanceFromBottom <= RunDockComponent.STICK_THRESHOLD_PX;
  }

  private _initialExpanded(): boolean {
    try {
      const raw = localStorage.getItem(this.storageKey);
      if (raw === 'true') return true;
      if (raw === 'false') return false;
    } catch {
      // localStorage unavailable (private mode, quota); fall through to
      // the collapsed default.
    }
    return false;
  }

  private _persistExpanded(value: boolean): void {
    try {
      localStorage.setItem(this.storageKey, String(value));
    } catch {
      // No persistence — state still works for the current session.
    }
  }
}
