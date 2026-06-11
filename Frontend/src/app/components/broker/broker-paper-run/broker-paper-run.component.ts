import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { rxResource } from '@angular/core/rxjs-interop';
import { from, of, switchMap, timer } from 'rxjs';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { LiveRunsService } from '../../../services/live-runs.service';
import type {
  CommandsSummary,
  CommandVerb,
  DesiredState,
  DesiredStateAction,
  HostRunnerHealth,
  HydratePolicy,
  LiveRunStatus,
  LiveRunSummary,
  LogLine,
  RunState,
} from '../../../api/live-runs.types';
import { fmtTimestampNy, fmtInteger } from '../format';
import { DesiredStateCardComponent } from './desired-state-card.component';
import { CommandPanelComponent } from './command-panel.component';

type FilterChip = 'today' | 'last14' | 'halted' | 'complete' | 'all';

const ACTIVE_STATES: ReadonlySet<RunState> = new Set([
  'running',
  'warming_up',
  'waiting_for_bars',
  'stale',
]);

function startOfTodayNyMs(): number {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now);
  const y = Number(parts.find((p) => p.type === 'year')?.value ?? '0');
  const mo = Number(parts.find((p) => p.type === 'month')?.value ?? '1') - 1;
  const d = Number(parts.find((p) => p.type === 'day')?.value ?? '1');
  const roughUtc = Date.UTC(y, mo, d);
  const checkStr = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(roughUtc));
  const [hh, mm] = checkStr.split(':').map(Number);
  // roughUtc is midnight UTC on the NY calendar date.
  // At that instant, NY shows HH:MM — so NY midnight is (24-HH) hours later.
  return roughUtc + (24 - hh) * 3_600_000 - mm * 60_000;
}

const FOURTEEN_DAYS_MS = 14 * 24 * 60 * 60 * 1000;
const WARMUP_BARS_REQUIRED = 15;
const DAEMON_LAUNCH_COMMAND = "$env:PYTHONPATH='PythonDataService'; python -m app.engine.live.host_daemon --repo-root .";

const EMPTY_DESIRED_STATE: DesiredState = {
  state: null,
  updated_at_ms: null,
  updated_by: null,
  reason: null,
  version: null,
  path_status: 'unknown_no_ledger_binding',
};

const EMPTY_COMMANDS: CommandsSummary = {
  entries: [],
  poll_interval_ms: 1_000,
};

const DESIRED_ACTION_PAST_TENSE: Record<DesiredStateAction, string> = {
  pause: 'paused',
  resume: 'resumed',
  stop: 'stopped',
};

function isHydratePolicy(value: string): value is HydratePolicy {
  return value === 'require' || value === 'optional' || value === 'disabled';
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : null;
}

@Component({
  selector: 'app-broker-paper-run',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PageHeaderComponent,
    SectionErrorComponent,
    JsonPipe,
    DesiredStateCardComponent,
    CommandPanelComponent,
  ],
  templateUrl: './broker-paper-run.component.html',
  styleUrl: './broker-paper-run.component.scss',
})
export class BrokerPaperRunComponent {
  private readonly svc = inject(LiveRunsService);

  // ── Run picker state ──────────────────────────────────────────────────
  readonly activeFilter = signal<FilterChip>('today');
  readonly selectedRunId = signal<string | null>(null);

  readonly runList = resource({
    loader: () => this.svc.listRuns({ limit: 50 }),
  });

  readonly filteredRuns = computed<LiveRunSummary[]>(() => {
    const runs = this.runList.value() ?? [];
    const filter = this.activeFilter();
    const now = Date.now();
    switch (filter) {
      case 'today':
        return runs.filter((r) => r.created_at_ms >= startOfTodayNyMs());
      case 'last14':
        return runs.filter((r) => r.created_at_ms >= now - FOURTEEN_DAYS_MS);
      case 'halted':
        return runs.filter((r) => r.state === 'halted');
      case 'complete':
        return runs.filter((r) => r.state === 'complete');
      default:
        return runs;
    }
  });

  // ── Status polling ────────────────────────────────────────────────────
  readonly status = resource({
    params: () => this.selectedRunId(),
    loader: ({ params: runId }): Promise<LiveRunStatus | null> => {
      if (!runId) return Promise.resolve(null);
      return this.svc.getStatus(runId);
    },
    defaultValue: null as LiveRunStatus | null,
  });

  // Per-run command channel (UI-4). Declared alongside the other resources.
  // A reload-tick signal is folded into `params` so we can force a refetch
  // by bumping the tick — `resource().reload()` is lazily attached only
  // after the resource is first read in a reactive context, which is not
  // guaranteed at the moment an operator action fires.
  private readonly commandsReloadTick = signal<number>(0);

  readonly commandsRes = resource({
    params: () => ({ runId: this.selectedRunId(), tick: this.commandsReloadTick() }),
    loader: ({ params }): Promise<CommandsSummary | null> => {
      if (!params.runId) return Promise.resolve(null);
      return this.svc.getCommands(params.runId);
    },
    defaultValue: null as CommandsSummary | null,
  });

  private reloadCommands(): void {
    this.commandsReloadTick.update((n) => n + 1);
  }

  private readonly pollIntervalMs = computed<number>(() => {
    const state = this.status.value()?.state;
    return state === 'idle' || state === 'complete' ? 10_000 : 5_000;
  });

  // ── Log-tail polling ──────────────────────────────────────────────────
  readonly logTail = rxResource({
    params: () => this.selectedRunId(),
    stream: ({ params: runId }) => {
      if (!runId) return of<LogLine[]>([]);
      return timer(0, 10_000).pipe(
        switchMap(() => from(this.svc.getLogTail(runId, 200))),
      );
    },
  });

  // ── Desired state + provenance (UI-2 / UI-3) ─────────────────────────
  readonly desiredState = computed<DesiredState>(
    () => this.status.value()?.desired_state ?? EMPTY_DESIRED_STATE,
  );

  readonly strategyInstanceId = computed<string | null>(
    () => this.status.value()?.strategy_instance_id ?? null,
  );

  readonly barSource = computed<string | null>(() => this.status.value()?.bar_source ?? null);

  /** Durable intent, resolved for display (UI-2). Never a guess. */
  readonly desiredStateLabel = computed<string>(() => {
    const d = this.desiredState();
    switch (d.path_status) {
      case 'unknown_no_ledger_binding':
        return 'unknown';
      case 'corrupt':
        return 'corrupt';
      case 'absent':
        return 'RUNNING';
      default:
        return d.state ?? 'RUNNING';
    }
  });

  readonly desiredBusy = signal<boolean>(false);
  readonly desiredError = signal<string | null>(null);

  // ── Command channel (UI-4) ───────────────────────────────────────────
  readonly commands = computed<CommandsSummary>(
    () => this.commandsRes.value() ?? EMPTY_COMMANDS,
  );

  readonly commandBusyVerb = signal<CommandVerb | null>(null);
  readonly commandError = signal<string | null>(null);
  readonly nowMs = signal<number>(Date.now());

  /**
   * Command-channel verbs are addressed by `run_id`, so they only require a
   * selected run — unlike durable intent they do not depend on the ledger's
   * `strategy_instance_id` binding. Disable when no run is selected.
   */
  readonly commandControlsDisabled = computed<boolean>(
    () => this.selectedRunId() == null,
  );

  // ── Host runner daemon ───────────────────────────────────────────────
  readonly runnerReadonly = signal<boolean>(true);
  readonly runnerHydratePolicy = signal<HydratePolicy>('require');
  readonly runnerBusy = signal<boolean>(false);
  readonly runnerMessage = signal<string | null>(null);
  readonly runnerError = signal<string | null>(null);
  readonly daemonLaunchCommand = DAEMON_LAUNCH_COMMAND;

  readonly daemonHealth = resource({
    loader: (): Promise<HostRunnerHealth> => this.svc.getHostRunnerHealth(),
  });

  readonly daemonUnavailable = computed<boolean>(() => this.daemonHealth.error() != null);

  readonly hostProcessLabel = computed<string>(() => {
    const health = this.daemonHealth.value();
    if (!health) return 'unknown';
    const process = health.process;
    if (process.run_id && process.run_id !== this.selectedRunId()) {
      return `${process.state} (${process.run_id.slice(0, 8)})`;
    }
    return process.state;
  });

  readonly hostProcessActiveForSelectedRun = computed<boolean>(() => {
    const process = this.daemonHealth.value()?.process;
    return process?.state === 'running' && process.run_id === this.selectedRunId();
  });

  readonly hostRunnerCanStart = computed<boolean>(() => {
    const health = this.daemonHealth.value();
    if (!health) return false;
    const state = health.process.state;
    return (
      !this.runnerBusy()
      && !this.daemonUnavailable()
      && this.selectedRunId() != null
      && state !== 'running'
      && state !== 'stopping'
    );
  });

  readonly hostRunnerCanStop = computed<boolean>(() => {
    return !this.runnerBusy() && this.hostProcessActiveForSelectedRun();
  });

  // ── Computed display helpers ──────────────────────────────────────────
  readonly topStripDynamicClasses = computed<string>(() => {
    const parts: string[] = [];
    const state = this.status.value()?.state;
    if (state) parts.push(`state-${state}`);
    if (this.actionRequired()) parts.push('action-required');
    return parts.join(' ');
  });

  readonly lastBarAgeLabel = computed<string>(() => {
    const s = this.status.value();
    if (!s || s.last_bar_age_s == null) return '—';
    const secs = Math.round(s.last_bar_age_s);
    if (secs < 60) return `${secs} s ago`;
    return `${Math.round(secs / 60)} m ago`;
  });

  readonly lastBarAgeStale = computed<boolean>(() => {
    const s = this.status.value();
    return s != null && s.last_bar_age_s != null && s.last_bar_age_s > 90;
  });

  readonly actionRequired = computed<boolean>(() => {
    const s = this.status.value();
    if (!s) return false;
    return s.state === 'halted' || s.state === 'poisoned' || s.state === 'stopped';
  });

  readonly warmupProgress = computed<string>(() => {
    const s = this.status.value();
    if (!s) return '—';
    return `${s.decisions.row_count} / ${WARMUP_BARS_REQUIRED}`;
  });

  readonly runIdShort = computed<string>(() => {
    const id = this.selectedRunId();
    return id ? id.slice(0, 8) : '—';
  });

  // ── Formatters (expose to template) ──────────────────────────────────
  readonly fmtTimestampNy = fmtTimestampNy;
  readonly fmtInteger = fmtInteger;

  // ── Filter chips ──────────────────────────────────────────────────────
  readonly filterChips: { key: FilterChip; label: string }[] = [
    { key: 'today', label: 'Today' },
    { key: 'last14', label: 'Last 14 days' },
    { key: 'halted', label: 'Halted' },
    { key: 'complete', label: 'Completed' },
    { key: 'all', label: 'All' },
  ];

  constructor() {
    // Auto-select the first active run when the list loads
    effect(() => {
      const runs = this.runList.value();
      if (runs == null || runs.length === 0 || this.selectedRunId() !== null) return;
      const active = runs.find((r) => ACTIVE_STATES.has(r.state));
      this.selectedRunId.set((active ?? runs[0]).run_id);
    });

    // Adaptive polling: only runs when a run is selected; onCleanup handles re-run and destroy
    effect((onCleanup) => {
      if (!this.selectedRunId()) return;
      const interval = this.pollIntervalMs();
      const id = setInterval(() => this.status.reload(), interval);
      onCleanup(() => clearInterval(id));
    });

    effect((onCleanup) => {
      if (!this.selectedRunId()) return;
      const id = setInterval(() => this.daemonHealth.reload(), 5_000);
      onCleanup(() => clearInterval(id));
    });

    // Command timeline + wall-clock tick for staleness detection (UI-4).
    effect((onCleanup) => {
      if (!this.selectedRunId()) return;
      const id = setInterval(() => {
        this.reloadCommands();
        this.nowMs.set(Date.now());
      }, 2_000);
      onCleanup(() => clearInterval(id));
    });
  }

  selectRun(runId: string): void {
    this.selectedRunId.set(runId);
  }

  setFilter(chip: FilterChip): void {
    this.activeFilter.set(chip);
  }

  copyRunId(): void {
    const id = this.selectedRunId();
    if (id) void navigator.clipboard.writeText(id);
  }

  copyDaemonCommand(): void {
    void navigator.clipboard.writeText(this.daemonLaunchCommand);
  }

  reloadRunList(): void {
    this.runList.reload();
  }

  /**
   * UI-3 — write durable operator intent, then reload status so the UI
   * reflects the new desired state. Errors surface on the card.
   */
  async issueDesiredState(action: DesiredStateAction): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId) return;
    this.desiredBusy.set(true);
    this.desiredError.set(null);
    try {
      await this.svc.writeDesiredState(runId, { action });
      this.status.reload();
    } catch (err) {
      this.desiredError.set(
        this.formatError(err, `Could not ${DESIRED_ACTION_PAST_TENSE[action]} the strategy.`),
      );
    } finally {
      this.desiredBusy.set(false);
    }
  }

  /**
   * UI-4 — write a per-run command-channel verb, then reload the command
   * timeline so the queued entry appears immediately.
   */
  async issueCommand(verb: CommandVerb): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId) return;
    this.commandBusyVerb.set(verb);
    this.commandError.set(null);
    try {
      await this.svc.writeCommand(runId, { verb });
      this.reloadCommands();
      this.nowMs.set(Date.now());
    } catch (err) {
      this.commandError.set(this.formatError(err, `Could not queue ${verb} command.`));
    } finally {
      this.commandBusyVerb.set(null);
    }
  }

  setRunnerReadonly(event: Event): void {
    this.runnerReadonly.set((event.target as HTMLInputElement).checked);
  }

  setRunnerHydratePolicy(value: string): void {
    if (isHydratePolicy(value)) this.runnerHydratePolicy.set(value);
  }

  async startHostRunner(): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId) return;
    this.runnerBusy.set(true);
    this.runnerError.set(null);
    this.runnerMessage.set(null);
    try {
      const response = await this.svc.startHostRunner(runId, {
        readonly: this.runnerReadonly(),
        hydrate_policy: this.runnerHydratePolicy(),
        strategy: 'spy_ema_crossover',
        max_orders_per_day: 50_000,
        ibkr_host: '127.0.0.1',
      });
      this.runnerMessage.set(response.process.message ?? 'Host runner start accepted.');
      this.daemonHealth.reload();
      this.status.reload();
    } catch (err) {
      this.runnerError.set(this.formatError(err));
    } finally {
      this.runnerBusy.set(false);
    }
  }

  async stopHostRunner(force = false): Promise<void> {
    const runId = this.selectedRunId();
    if (!runId) return;
    this.runnerBusy.set(true);
    this.runnerError.set(null);
    this.runnerMessage.set(null);
    try {
      const response = await this.svc.stopHostRunner(runId, { force });
      this.runnerMessage.set(response.process.message ?? 'Host runner stop requested.');
      this.daemonHealth.reload();
      this.status.reload();
    } catch (err) {
      this.runnerError.set(this.formatError(err));
    } finally {
      this.runnerBusy.set(false);
    }
  }

  fmtBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }


  getField(obj: Record<string, unknown> | null, key: string): unknown {
    return obj != null ? (obj[key] ?? null) : null;
  }

  formatField(obj: Record<string, unknown> | null, key: string): string {
    const val = this.getField(obj, key);
    return val != null ? String(val) : '—';
  }

  getFillTs(fill: Record<string, unknown>): number | null {
    const v = fill['ts_ms'];
    return typeof v === 'number' ? v : null;
  }

  getSelectValue(event: Event): string {
    return (event.target as HTMLSelectElement).value;
  }

  private formatError(err: unknown, fallback = 'Host runner action failed.'): string {
    const record = asRecord(err);
    const errorPayload = asRecord(record?.['error']);
    const detail = errorPayload?.['detail'];
    if (typeof detail === 'string') return detail;
    const message = record?.['message'];
    if (typeof message === 'string') return message;
    return fallback;
  }
}
