import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import type {
  CommandEntry,
  CommandsSummary,
  CommandVerb,
} from '../../../api/live-runs.types';
import { fmtTimestampNy } from '../format';

interface CommandButton {
  verb: CommandVerb;
  label: string;
  tone: 'neutral' | 'warn' | 'danger';
}

interface CommandRow extends CommandEntry {
  ageMs: number | null;
  stale: boolean;
}

const COMMAND_BUTTONS: readonly CommandButton[] = [
  { verb: 'PAUSE', label: 'Pause', tone: 'warn' },
  { verb: 'RESUME', label: 'Resume', tone: 'neutral' },
  { verb: 'STOP', label: 'Stop', tone: 'danger' },
  { verb: 'FLATTEN', label: 'Flatten', tone: 'danger' },
  { verb: 'MARK_POISONED', label: 'Mark Poisoned', tone: 'danger' },
  { verb: 'RECONCILE', label: 'Reconcile', tone: 'neutral' },
];

/** A pending command older than this many poll intervals is "stale". */
const STALE_POLL_MULTIPLIER = 3;

/**
 * UI-4 — Per-run command-channel controls + pending→ack timeline.
 *
 * Buttons write a command-channel verb (the parent owns the write + reload).
 * The timeline renders real command files: `queued` while only a pending
 * file exists, `acknowledged`/`failed` once the ack file lands. A queued
 * command whose age exceeds three poll intervals is flagged `stale` so an
 * unresponsive bot is visually obvious.
 *
 * `nowMs` is passed in by the parent (driven off the status poll) rather
 * than read from a global clock, keeping staleness deterministic in tests.
 */
@Component({
  selector: 'app-command-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './command-panel.component.html',
  styleUrl: './command-panel.component.scss',
})
export class CommandPanelComponent {
  readonly commands = input.required<CommandsSummary>();
  readonly nowMs = input.required<number>();
  readonly busyVerb = input<CommandVerb | null>(null);
  readonly disabled = input<boolean>(false);
  readonly writeError = input<string | null>(null);

  readonly issue = output<CommandVerb>();

  readonly fmtTimestampNy = fmtTimestampNy;
  readonly buttons = COMMAND_BUTTONS;

  private readonly staleThresholdMs = computed<number>(
    () => this.commands().poll_interval_ms * STALE_POLL_MULTIPLIER,
  );

  /** Newest-first timeline rows with derived age + staleness. */
  readonly rows = computed<CommandRow[]>(() => {
    const now = this.nowMs();
    const threshold = this.staleThresholdMs();
    return [...this.commands().entries]
      .sort((a, b) => b.seq - a.seq)
      .map((entry) => {
        const ageMs =
          entry.queued_at_ms != null ? Math.max(0, now - entry.queued_at_ms) : null;
        const stale =
          entry.status === 'queued' && ageMs != null && ageMs > threshold;
        return { ...entry, ageMs, stale };
      });
  });

  readonly hasRows = computed<boolean>(() => this.rows().length > 0);

  readonly hasStale = computed<boolean>(() => this.rows().some((r) => r.stale));

  ageLabel(ageMs: number | null): string {
    if (ageMs == null) return '—';
    const secs = Math.round(ageMs / 1000);
    if (secs < 60) return `${secs}s`;
    return `${Math.round(secs / 60)}m`;
  }

  statusClass(row: CommandRow): string {
    if (row.stale) return 'status-stale';
    switch (row.status) {
      case 'acknowledged':
        return 'status-acknowledged';
      case 'failed':
        return 'status-failed';
      default:
        return 'status-queued';
    }
  }

  statusLabel(row: CommandRow): string {
    if (row.stale) return 'stale';
    return row.status;
  }

  isBusy(verb: CommandVerb): boolean {
    return this.busyVerb() === verb;
  }

  issueCommand(verb: CommandVerb): void {
    if (this.disabled() || this.busyVerb() != null) return;
    this.issue.emit(verb);
  }
}
