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
import { RunSessionService, RunLogEntry } from '../../../services/run-session.service';

/**
 * Bottom-docked run UI for the data lab.
 *
 * Replaces the previous in-flow run-card. Pinned to the viewport bottom,
 * always present in the page, toggles between an expanded panel
 * (320 px tall, structured progress strip + scrolling event log) and a
 * collapsed status strip (~36 px tall, single-line "what's running").
 *
 * The log is sourced from RunSessionService.log — a rolling FIFO of
 * the last 500 SSE events across runs. Auto-scroll is terminal-style:
 * always sticks to the bottom on each new entry. The user can pause
 * auto-scroll implicitly by scrolling up; we re-stick once they scroll
 * back to within a few lines of the bottom.
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
  readonly runSession = inject(RunSessionService);

  /** Open by default — the dock is the run UI now, not a sidecar. */
  readonly expanded = signal(true);

  private readonly logScroll = viewChild<ElementRef<HTMLDivElement>>('logScroll');

  /** Stay-at-bottom flag. Flipped to false when the user scrolls up
   *  past the threshold; flipped back to true when they scroll near
   *  the bottom again. */
  private stickToBottom = true;
  private static readonly STICK_THRESHOLD_PX = 24;

  toggle(): void {
    this.expanded.update((v) => !v);
  }

  expand(): void {
    this.expanded.set(true);
  }

  collapse(): void {
    this.expanded.set(false);
  }

  clearLog(): void {
    this.runSession.clearLog();
  }

  cancel(): void {
    void this.runSession.cancel();
  }

  /** Pretty time stamp for log lines: HH:mm:ss.ms (24h, local). */
  formatTime(ms: number): string {
    const d = new Date(ms);
    const pad = (n: number, w = 2) => n.toString().padStart(w, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }

  /** Track-by for @for so DOM nodes are stable across appends. */
  trackById(_: number, entry: RunLogEntry): string {
    return entry.id;
  }

  /** Headline summarising the current run state for the collapsed
   *  strip and the expanded header. */
  readonly headline = computed<string>(() => {
    const state = this.runSession.state();
    const result = this.runSession.result();
    const error = this.runSession.error();
    const chunks = this.runSession.chunks();
    const components = this.runSession.bundleComponents();
    if (state === 'idle') return 'idle — no run in flight';
    if (state === 'fetching') {
      if (chunks.length === 0) return 'fetching · planning chunks';
      const done = chunks.filter((c) => c.status === 'done').length;
      const fetching = chunks.find((c) => c.status === 'fetching');
      const idx = fetching?.index ?? Math.min(done + 1, chunks.length);
      return `fetching · chunk ${idx} of ${chunks.length}`;
    }
    if (state === 'bundling') {
      if (components.length === 0) return 'bundling · packaging';
      const done = components.filter((c) => c.status === 'done').length;
      return `bundling · ${done} of ${components.length} components`;
    }
    if (state === 'done' && result) return `done · ${result.filename}`;
    if (state === 'error' && error) return `error · ${error.message}`;
    return state;
  });

  /** Severity tag for the headline strip. */
  readonly headlineLevel = computed<RunLogEntry['level']>(() => {
    const state = this.runSession.state();
    if (state === 'done') return 'success';
    if (state === 'error') {
      return this.runSession.error()?.kind === 'cancelled' ? 'warn' : 'error';
    }
    if (state === 'fetching' || state === 'bundling') return 'info';
    return 'info';
  });

  /** Whole-percent for the progress bar + aria-valuenow. */
  readonly progressPercent = computed<number>(() => Math.round(this.runSession.progressFraction() * 100));

  /** Human-friendly ETA for the headline strip. */
  readonly etaText = computed<string | null>(() => {
    const eta = this.runSession.etaSeconds();
    if (eta === null) return null;
    if (eta < 60) return `~${eta} s`;
    const mins = Math.floor(eta / 60);
    const secs = eta % 60;
    return `~${mins} m ${secs.toString().padStart(2, '0')} s`;
  });

  readonly canCancel = computed<boolean>(() => {
    const s = this.runSession.state();
    return s === 'fetching' || s === 'bundling';
  });

  constructor() {
    // Auto-scroll the log to the bottom whenever a new entry lands,
    // unless the user has scrolled up to read history. The signal read
    // makes the effect run on every log mutation.
    effect(() => {
      // Touch the log so this effect re-runs on append.
      this.runSession.log();
      const el = this.logScroll()?.nativeElement;
      if (!el) return;
      if (!this.stickToBottom) return;
      // Defer so the DOM has time to commit the appended <li> nodes.
      queueMicrotask(() => {
        el.scrollTop = el.scrollHeight;
      });
    });

    // Publish the dock's current height to a CSS variable on :root so
    // the data-lab page can reserve bottom padding equal to whatever
    // the dock is occupying right now (expanded 320 px vs collapsed
    // 36 px). Otherwise the last form controls hide under the dock.
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
}
