import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

export interface PhaseRailStop {
  label: string;
  ids: readonly string[];
}

import { ButtonModule } from 'primeng/button';
import { ProgressBarModule } from 'primeng/progressbar';
import { TagModule } from 'primeng/tag';

import { JobState } from '../../../../services/jobs.service';
import { RunLogEntry } from '../../../../utils/run-log-buffer';

type Severity = 'info' | 'success' | 'warn' | 'danger' | 'secondary';

/**
 * Live-progress panel shared across feature-runner, signal-engine, and
 * cross-sectional batch UIs. Renders four sections, top-to-bottom:
 *   1. status pill (Queued / Running / Completed / Failed / Cancelled)
 *   2. current phase label + progress bar
 *   3. scrolling log feed (auto-tails)
 *   4. cancel button (only while running)
 *
 * The panel is purely a view — state lives in the parent component (so
 * a parent can decide to hide the panel entirely on cache hits, for
 * example). All inputs are signals.
 */
@Component({
  selector: 'app-run-progress-panel',
  imports: [CommonModule, ButtonModule, ProgressBarModule, TagModule],
  templateUrl: './run-progress-panel.component.html',
  styleUrls: ['./run-progress-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RunProgressPanelComponent {
  /** Current job state, or null when no run is in flight. The panel
   *  collapses to nothing when null; parents can also choose to render
   *  conditionally. */
  readonly job = input<JobState | null>(null);

  /** Live log entries. Pass the result of ``RunLogBuffer.entries()``. */
  readonly logs = input<readonly RunLogEntry[]>([]);

  /** Optional override for the panel title. Defaults to "Live progress". */
  readonly title = input<string>('Live progress');

  /** Auto-scroll-to-bottom toggle is on by default. The user can flip
   *  it off to read older lines without the feed pulling them away. */
  readonly autoScroll = input<boolean>(true);

  /** Optional phase rail definition. When provided, a horizontal stop-rail
   *  renders above the log feed showing Fetch → Walk-forward → etc.
   *  Each stop's ``ids`` list maps the job's ``phase`` field to that stop.
   *  Leave empty (default) to hide the rail entirely. */
  readonly phases = input<readonly PhaseRailStop[]>([]);

  /** Emitted when the user clicks Cancel. Parent calls JobsService.cancelJob.
   *  Named ``cancelRun`` (not ``cancel``) to avoid shadowing the native
   *  DOM ``cancel`` event in template bindings. */
  readonly cancelRun = output();

  private readonly logsContainer = viewChild<ElementRef<HTMLDivElement>>('logsContainer');

  readonly running = computed<boolean>(() => {
    const j = this.job();
    return j?.status === 'queued' || j?.status === 'running';
  });

  readonly statusLabel = computed<string>(() => {
    const j = this.job();
    if (!j) return '';
    switch (j.status) {
      case 'queued':
        return 'Queued';
      case 'running':
        return 'Running';
      case 'completed':
        return 'Completed';
      case 'failed':
        return 'Failed';
      case 'cancelled':
        return 'Cancelled';
    }
  });

  readonly statusSeverity = computed<Severity>(() => {
    const j = this.job();
    if (!j) return 'secondary';
    switch (j.status) {
      case 'completed':
        return 'success';
      case 'failed':
        return 'danger';
      case 'cancelled':
        return 'warn';
      case 'running':
        return 'info';
      default:
        return 'secondary';
    }
  });

  readonly phaseLabel = computed<string>(() => {
    const j = this.job();
    if (!j) return '';
    return j.phaseLabel ?? j.phase ?? '';
  });

  readonly progressPercent = computed<number>(() => {
    const j = this.job();
    if (!j || !j.total || j.total === 0) return 0;
    return Math.min(100, Math.round(((j.current ?? 0) / j.total) * 100));
  });

  readonly progressDetail = computed<string>(() => {
    const j = this.job();
    if (!j || !j.total) return '';
    const unit = j.unit ?? 'items';
    const current = j.current ?? 0;
    return `${current.toLocaleString()} / ${j.total.toLocaleString()} ${unit}`;
  });

  readonly activeStopIndex = computed<number>(() => {
    const j = this.job();
    const stops = this.phases();
    if (!stops.length) return -1;
    if (j?.status === 'completed') return stops.length;
    const phaseId = j?.phase ?? '';
    const idx = stops.findIndex(s => s.ids.includes(phaseId));
    return idx >= 0 ? idx : -1;
  });

  // Reactive ``now`` that ticks every second while a job is running so
  // ``elapsedSeconds`` advances even between SSE events. Without this,
  // long phases (e.g. a 30-second IV history fetch) would freeze the
  // displayed elapsed time until the next ``job.phase``/``job.log``
  // update arrived.
  private readonly now = signal(Date.now());
  private tickHandle: number | null = null;

  readonly elapsedSeconds = computed<number | null>(() => {
    const j = this.job();
    if (!j?.startedAt) return null;
    const end = j.finishedAt ?? this.now();
    return Math.max(0, Math.round((end - j.startedAt) / 1000));
  });

  constructor() {
    const destroyRef = inject(DestroyRef);
    destroyRef.onDestroy(() => this.stopTicking());

    // Auto-scroll the log container to the bottom whenever a new entry
    // lands AND the autoScroll input is on.
    effect(() => {
      this.logs(); // dependency
      if (!this.autoScroll()) return;
      const el = this.logsContainer()?.nativeElement;
      if (!el) return;
      // Defer to next microtask so the DOM has the new row.
      queueMicrotask(() => {
        el.scrollTop = el.scrollHeight;
      });
    });

    // Drive the elapsed-time ticker from the running state. Starts on
    // the first event that says the job is running (and has a
    // ``startedAt``); stops on terminal status or when ``finishedAt``
    // is set, so a finished job shows a frozen elapsed value.
    effect(() => {
      const j = this.job();
      const shouldTick = !!j?.startedAt && !j?.finishedAt && this.running();
      if (shouldTick) {
        this.startTicking();
      } else {
        this.stopTicking();
      }
    });
  }

  private startTicking(): void {
    if (this.tickHandle !== null) return;
    this.tickHandle = setInterval(() => this.now.set(Date.now()), 1000) as unknown as number;
  }

  private stopTicking(): void {
    if (this.tickHandle !== null) {
      clearInterval(this.tickHandle);
      this.tickHandle = null;
    }
  }

  formatTime(ts: number): string {
    return new Date(ts).toLocaleTimeString('en-US', { hour12: false });
  }

  logRowClass(level: RunLogEntry['level']): string {
    return `log-row log-${level}`;
  }

  onCancelClick(): void {
    this.cancelRun.emit();
  }
}
