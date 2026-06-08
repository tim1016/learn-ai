import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { rxResource } from '@angular/core/rxjs-interop';
import { from, of, timer } from 'rxjs';
import { switchMap } from 'rxjs/operators';
import type { LogLine } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { fmtTimestampNy } from '../format';

/** How many tail lines to request. The backend caps at 1000; 500 is enough to
 * see a crash sequence without flooding the dialog. */
const TAIL_LINES = 500;

/**
 * Modal viewer for a run's `live.log` tail. The subject is a single `run_id`
 * (the crashed last run, or the bound live run). A terminated run is fetched
 * once (re-fetch via Refresh); a live run polls every 5s so the dialog tracks
 * an active session. The full `run_id` is shown with copy-to-clipboard — the
 * link back to the run, since the run-spine detail page was retired (#400).
 */
@Component({
  selector: 'app-broker-run-log-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-run-log-modal.component.html',
  styleUrl: './broker-run-log-modal.component.scss',
  host: {
    '(keydown.escape)': 'closed.emit()',
    '(keydown.tab)': 'trapFocus($event, false)',
    '(keydown.shift.tab)': 'trapFocus($event, true)',
  },
})
export class BrokerRunLogModalComponent {
  private readonly svc = inject(LiveRunsService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly runId = input.required<string>();
  /** Poll while the run is still producing output; fetch once when terminated. */
  readonly live = input<boolean>(false);
  readonly closed = output();

  readonly fmtTimestampNy = fmtTimestampNy;
  readonly copied = signal<boolean>(false);

  private readonly refreshTick = signal(0);
  private readonly closeButton =
    viewChild<ElementRef<HTMLButtonElement>>('closeBtn');
  private readonly logBody = viewChild<ElementRef<HTMLElement>>('logBody');

  constructor() {
    // Move focus into the dialog when it mounts so Escape works and focus is
    // contained at the close control (WCAG focus management).
    effect(() => {
      this.closeButton()?.nativeElement.focus();
    });
    // The backend returns the tail oldest-first, so the crash sequence is at
    // the end. Stick to the bottom on every (re)load so the operator opening
    // "why it crashed" sees the latest lines, not 500 lines of history.
    effect(() => {
      const lines = this.logTail.value();
      const el = this.logBody()?.nativeElement;
      if (el && lines?.length) el.scrollTop = el.scrollHeight;
    });
  }

  readonly logTail = rxResource<LogLine[], string>({
    params: () => `${this.runId()}:${this.refreshTick()}`,
    stream: () => {
      const runId = this.runId();
      if (!runId) return of<LogLine[]>([]);
      if (this.live()) {
        return timer(0, 5_000).pipe(
          switchMap(() => from(this.svc.getLogTail(runId, TAIL_LINES))),
        );
      }
      return from(this.svc.getLogTail(runId, TAIL_LINES));
    },
  });

  refresh(): void {
    this.refreshTick.update((n) => n + 1);
  }

  /** Keep keyboard focus inside the dialog: wrap from the last control back to
   * the first (and vice-versa). The backdrop is `tabindex=-1`, so it's excluded;
   * disabled controls (e.g. Refresh mid-load) are skipped too. */
  trapFocus(event: Event, backward: boolean): void {
    const root = this.host.nativeElement;
    const focusable = Array.from(
      root.querySelectorAll<HTMLElement>('button, [href], input, [tabindex]'),
    ).filter((el) => el.tabIndex >= 0 && !el.hasAttribute('disabled'));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = root.ownerDocument.activeElement;
    if (backward && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!backward && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async copyRunId(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.runId());
      this.copied.set(true);
    } catch {
      // Clipboard API is unavailable in insecure contexts or when permission is
      // denied. The id is visible and selectable, so degrade silently rather
      // than surface a failure the operator can route around by hand.
    }
  }
}
