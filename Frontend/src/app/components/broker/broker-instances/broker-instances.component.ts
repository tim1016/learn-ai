import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  DecisionColumnDescriptor,
  DesiredStateAction,
  FleetContamination,
  InstanceBrokerView,
  IntentActuation,
  LiveInstanceSummary,
  LiveInstanceStatus,
  ReadinessGate,
  ReadinessVector,
} from '../../../api/live-instances.types';
import type { CommandEntry, CommandVerb } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { BrokerStartStopCardComponent } from '../broker-start-stop-card/broker-start-stop-card.component';
import { type OperationError, type OperationKind, toOperationError } from '../operation-error';

// Advanced command verb -> operation kind for the error map.
const VERB_TO_KIND: Record<CommandVerb, OperationKind> = {
  RECONCILE: 'reconcile',
  FLATTEN: 'flatten',
  MARK_POISONED: 'mark-poisoned',
  PAUSE: 'pause',
  RESUME: 'resume',
  STOP: 'stop',
};

interface HeroStatus {
  label: string;
  tone: 'ok' | 'warn' | 'bad' | 'unknown';
  detail: string;
}

interface HealthRow {
  icon: string;
  label: string;
  status: string;
  tone: 'ok' | 'warn' | 'bad';
  technicalKey: string;
  guide: string;
}

interface ChecklistRow {
  key: string;
  label: string;
  status: 'pass' | 'fail' | 'unknown';
  detail: string;
  meaning: string;
  fix: string;
}

interface LastExitNotice {
  tone: 'ok' | 'warn' | 'bad';
  title: string;
  detail: string;
  fix: string;
}

interface IntentChoice {
  action: DesiredStateAction;
  icon: string;
  label: string;
  description: string;
}

interface AdvancedAction {
  verb: CommandVerb;
  label: string;
  description: string;
  tone: 'safe' | 'danger';
}

const INTENT_CHOICES: readonly IntentChoice[] = [
  { action: 'pause', icon: 'pi pi-pause', label: 'Pause', description: 'Bot will start but not place any orders' },
  { action: 'resume', icon: 'pi pi-play', label: 'Resume', description: 'Bot will start and trade normally' },
  { action: 'stop', icon: 'pi pi-stop', label: 'Stop', description: 'Bot will not start at all' },
];

const ADVANCED_ACTIONS: readonly AdvancedAction[] = [
  { verb: 'RECONCILE', label: 'Re-sync account balance with broker', description: 'Safe: refreshes what the bot believes your broker account contains.', tone: 'safe' },
  { verb: 'FLATTEN', label: 'Close all open positions immediately', description: 'Warning: sends a command to flatten positions for this running bot.', tone: 'danger' },
  { verb: 'MARK_POISONED', label: 'Flag this instance as unsafe and halt all trading', description: 'Warning: marks this instance unsafe until an operator investigates.', tone: 'danger' },
];

const GATE_LABELS: Record<string, { label: string; meaning: string; fix: string }> = {
  desired_state: {
    label: 'Bot Intent Set',
    meaning: 'You have told the bot whether it should pause, resume, or stay stopped.',
    fix: 'Choose Pause, Resume, or Stop in Bot Behavior.',
  },
  poison_sentinel: {
    label: 'No Emergency Stop',
    meaning: 'No safety halt was triggered for this bot.',
    fix: 'Open Advanced Actions only after reviewing the halt reason.',
  },
  prior_day_halt: {
    label: 'Yesterday Ended Cleanly',
    meaning: 'No issue carried over from the previous session.',
    fix: 'Review the prior session before restarting.',
  },
  latest_reconcile: {
    label: 'Account Reconciled',
    meaning: 'The bot confirmed your account balances match expectations.',
    fix: 'Run Re-sync account balance with broker.',
  },
  orders_cap: {
    label: 'Daily Trade Limit Available',
    meaning: 'The bot has not used every trade allowed by today\'s safety cap.',
    fix: 'Wait for the next session or raise the safety cap before starting.',
  },
};

function titleizeKey(key: string): string {
  return key
    .split('_')
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

/**
 * Trader-facing instance control panel.
 */
@Component({
  selector: 'app-broker-instances',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    BrokerConnectivityStripComponent,
    BrokerOperationResultComponent,
    BrokerStartStopCardComponent,
  ],
  templateUrl: './broker-instances.component.html',
  styleUrl: './broker-instances.component.scss',
})
export class BrokerInstancesComponent {
  private readonly svc = inject(LiveRunsService);
  private readonly connectivity = inject(BrokerConnectivityService);

  readonly selectedInstanceId = signal<string | null>(null);

  readonly fleet = resource({
    loader: () => this.svc.getInstances(),
  });

  readonly status = resource({
    params: () => this.selectedInstanceId() ?? undefined,
    loader: ({ params }) => this.svc.getInstanceStatus(params),
  });

  readonly instances = computed<LiveInstanceSummary[]>(() => this.fleet.value() ?? []);

  /** Account-level contamination (ADR 0005, #399). Backend-authored — the one
   * readiness signal no single engine can see. */
  readonly account = resource({ loader: () => this.svc.getAccountFleet() });
  readonly commands = resource({
    params: () => this.selectedInstanceId() ?? undefined,
    loader: ({ params }) => this.svc.getInstanceCommands(params),
  });
  readonly commandEntries = computed<CommandEntry[]>(() => this.commands.value()?.entries ?? []);

  readonly busyAction = signal<DesiredStateAction | null>(null);
  readonly lastActuation = signal<IntentActuation | null>(null);
  readonly busyVerb = signal<CommandVerb | null>(null);

  // Structured inline errors (handoff: inline-only surfacing, never a toast).
  readonly intentError = signal<OperationError | null>(null);
  readonly commandError = signal<OperationError | null>(null);
  readonly advancedOpen = signal<boolean>(false);
  readonly intentChoices = INTENT_CHOICES;
  readonly advancedActions = ADVANCED_ACTIONS;

  select(instanceId: string): void {
    this.selectedInstanceId.set(instanceId);
    this.lastActuation.set(null);
    this.intentError.set(null);
    this.commandError.set(null);
  }

  /**
   * The single operator intent knob: durable desired-state, actuated on the
   * live binding when present (ADR 0004). Liveness-independent — PAUSED means
   * "should not make new orders" whether it actuates now or gates the next start.
   */
  async setIntent(action: DesiredStateAction): Promise<void> {
    const id = this.selectedInstanceId();
    if (id === null) return;
    this.busyAction.set(action);
    this.intentError.set(null);
    try {
      const result = await this.svc.setInstanceDesiredState(id, { action });
      if (this.selectedInstanceId() === id) {
        this.lastActuation.set(result.actuation);
        this.status.reload();
      }
    } catch (err) {
      if (this.selectedInstanceId() === id) this.intentError.set(toOperationError(action, err));
    } finally {
      this.busyAction.set(null);
    }
  }

  /** A start/stop the daemon accepted (#416): refresh process state, the live
   * binding, and the connectivity strip's daemon-process signal. */
  onStartStopChanged(): void {
    this.status.reload();
    this.connectivity.reload();
  }

  /** Issue a one-shot command (FLATTEN/RECONCILE/MARK_POISONED) to the bound run (#397). */
  async issueCommand(verb: CommandVerb): Promise<void> {
    const id = this.selectedInstanceId();
    if (id === null) return;
    this.busyVerb.set(verb);
    this.commandError.set(null);
    try {
      await this.svc.issueInstanceCommand(id, { verb });
      if (this.selectedInstanceId() === id) this.commands.reload();
    } catch (err) {
      if (this.selectedInstanceId() === id) this.commandError.set(toOperationError(VERB_TO_KIND[verb], err));
    } finally {
      this.busyVerb.set(null);
    }
  }

  async issueAdvancedCommand(verb: CommandVerb): Promise<void> {
    if (verb === 'FLATTEN') {
      const ok = window.confirm('Are you sure? This will close all open positions managed by this bot.');
      if (!ok) return;
    }
    if (verb === 'MARK_POISONED') {
      const typed = window.prompt('Type HALT to flag this instance as unsafe and halt trading.');
      if (typed !== 'HALT') return;
    }
    await this.issueCommand(verb);
  }

  setAdvancedOpen(event: Event): void {
    if (event.target instanceof HTMLDetailsElement) {
      this.advancedOpen.set(event.target.open);
    }
  }

  heroStatus(s: LiveInstanceStatus): HeroStatus {
    if (s.process.state === 'unreachable') {
      return {
        label: 'UNKNOWN',
        tone: 'unknown',
        detail: 'The dashboard cannot determine the current bot state.',
      };
    }
    if (s.process.state === 'running' || s.process.state === 'stopping') {
      if (s.readiness?.verdict === 'READY' && this.currentIntent(s) === 'resume') {
        return {
          label: 'LIVE & TRADING',
          tone: 'ok',
          detail: 'Your bot is running and ready to place orders when the strategy signals.',
        };
      }
      return {
        label: 'RUNNING - NOT READY',
        tone: 'warn',
        detail: 'The process is on, but at least one pre-trade check needs attention.',
      };
    }
    return {
      label: 'STOPPED',
      tone: 'bad',
      detail: 'Your bot is not active. Press Start Trading to resume when the checklist is clear.',
    };
  }

  strategyTitle(s: LiveInstanceStatus): string {
    const raw = s.start_defaults?.strategy || s.strategy_instance_id;
    if (raw === 'spy_ema_crossover') return 'SPY EMA Crossover';
    return titleizeKey(raw);
  }

  shortId(value: string | null | undefined): string {
    if (!value) return '—';
    return value.length > 12 ? `${value.slice(0, 8)}...` : value;
  }

  accountBadge(acct: FleetContamination): string {
    if (acct.verdict === 'clean') return 'ALL POSITIONS ACCOUNTED FOR';
    if (acct.verdict === 'contaminated') return 'UNRECOGNIZED POSITIONS DETECTED';
    return 'ACCOUNT STATUS UNKNOWN';
  }

  healthRows(s: LiveInstanceStatus): HealthRow[] {
    const engineOk = s.process.state === 'running' || s.process.state === 'stopping';
    // Broker connectivity is a global fact (one IBKR session per gateway),
    // sourced from the live /api/broker/health probe via the connectivity
    // service — NOT from this instance's live_state sidecar. A bot that
    // crashed before writing its sidecar must not make the broker read
    // "NOT CONNECTED" while IBKR is in fact connected.
    const broker = this.connectivity.brokerState();
    const rulesOk = s.readiness?.verdict === 'READY';
    const rulesWarn = s.readiness?.verdict === 'DEGRADED' || s.readiness?.verdict === 'UNKNOWN';
    return [
      {
        icon: 'pi pi-cog',
        label: 'Trading Engine',
        status: engineOk ? 'RUNNING' : 'STOPPED',
        tone: engineOk ? 'ok' : 'bad',
        technicalKey: 'process.state',
        guide: 'This is the program that watches signals and manages the bot session.',
      },
      {
        icon: 'pi pi-link',
        label: 'Broker Connection (IBKR)',
        status: broker === 'ok' ? 'CONNECTED' : broker === 'unknown' ? 'CHECKING…' : 'NOT CONNECTED',
        tone: broker === 'ok' ? 'ok' : broker === 'unknown' ? 'warn' : 'bad',
        technicalKey: 'broker.connected',
        guide: 'Whether the dashboard holds a live IBKR session. Polled every 5 seconds, independent of any single bot — a stopped or crashed bot does not turn this red.',
      },
      {
        icon: 'pi pi-list-check',
        label: 'Trading Rules',
        status: rulesOk ? 'ALL CLEAR' : rulesWarn ? 'CHECK NEEDED' : 'BLOCKED',
        tone: rulesOk ? 'ok' : rulesWarn ? 'warn' : 'bad',
        technicalKey: 'readiness.verdict',
        guide: 'These checks decide whether the bot is allowed to act on the next signal.',
      },
    ];
  }

  /** Human "why did the last session stop?" surface. Reads the terminated
   * run's exit reason + hydration receipt (backend `last_exit`) so a STOPPED
   * instance explains itself instead of leaving the operator to read logs.
   * Returns null while the instance is live or nothing has run. */
  lastExitNotice(s: LiveInstanceStatus): LastExitNotice | null {
    const e = s.last_exit;
    if (!e) return null;
    if (s.process.state === 'running' || s.process.state === 'stopping') return null;

    const code = e.exit_code;
    // Cold-start / seed-day: hydration rejected because there is no prior state.
    if (e.hydration_accepted === false && e.hydration_failure_reason === 'missing') {
      return {
        tone: 'warn',
        title: 'Needs a seed day (no saved indicator state)',
        detail: 'The last run stopped because it had no saved indicator state to resume from — expected on a first run.',
        fix: 'Redeploy or start with Indicator State Hydration set to Optional to run a seed day. After one clean session it can use Required.',
      };
    }
    // Other hydration rejections (corrupt / stale / identity mismatch).
    if (e.hydration_accepted === false && e.hydration_failure_reason) {
      return {
        tone: 'bad',
        title: 'Saved indicator state could not be used',
        detail: `Indicator-state hydration was rejected (${e.hydration_failure_reason}).`,
        fix: 'Start with Hydration = Optional to cold-start if the saved state is no longer valid, and review the hydration receipt.',
      };
    }
    // Clean exits — informational, not alarming.
    if (e.exit_reason === 'normal' || e.exit_reason === 'force_flat_complete') {
      return {
        tone: 'ok',
        title: 'Last session ended cleanly',
        detail: `The previous run finished normally${code != null ? ` (exit ${code})` : ''}.`,
        fix: 'Press Start Trading to begin a new session.',
      };
    }
    if (e.exit_reason === 'max_orders_exceeded') {
      return {
        tone: 'warn',
        title: 'Daily order cap reached',
        detail: 'The last run stopped after hitting its max-orders-per-day limit.',
        fix: 'This resets next session. Raise the cap on redeploy if that was intentional.',
      };
    }
    // Generic failure (exception / signal / fatal_halt / unknown).
    return {
      tone: 'bad',
      title: 'Last session ended unexpectedly',
      detail: `The previous run exited${code != null ? ` with code ${code}` : ''}${e.exit_reason ? ` (${e.exit_reason})` : ''}.`,
      fix: 'Open the run log for the full detail.',
    };
  }

  checklistRows(r: ReadinessVector | null): ChecklistRow[] {
    if (!r) {
      return [
        {
          key: 'readiness',
          label: 'Pre-Trade Status Loaded',
          status: 'unknown',
          detail: 'No checklist is available yet.',
          meaning: 'The bot has not reported whether it can trade.',
          fix: 'Refresh the page or start the bot to load readiness.',
        },
      ];
    }
    return r.gates.map((gate) => this.checklistRow(gate));
  }

  checklistRow(gate: ReadinessGate): ChecklistRow {
    const copy = GATE_LABELS[gate.name] ?? {
      label: titleizeKey(gate.name),
      meaning: gate.detail,
      fix: 'Open the details for this check and review the latest bot status.',
    };
    return {
      key: gate.name,
      label: copy.label,
      status: gate.status,
      detail: gate.detail,
      meaning: copy.meaning,
      fix: copy.fix,
    };
  }

  checksPassed(r: ReadinessVector | null): number {
    return this.checklistRows(r).filter((row) => row.status === 'pass').length;
  }

  currentIntent(s: LiveInstanceStatus): DesiredStateAction | null {
    switch (s.desired_state?.state) {
      case 'PAUSED':
        return 'pause';
      case 'RUNNING':
        return 'resume';
      case 'STOPPED':
        return 'stop';
      default:
        return null;
    }
  }

  intentDisabledReason(s: LiveInstanceStatus): string | null {
    if (this.busyAction() !== null) return 'Saving preference...';
    if (!s.live_binding) return 'These take effect on the next start.';
    return null;
  }

  /** Format a decision-row value by its spec-declared format (#396). */
  formatCell(decision: Record<string, unknown> | null, col: DecisionColumnDescriptor): string {
    const value = decision?.[col.name];
    if (value === null || value === undefined) return '—';
    if (col.format === 'decimal' && typeof value === 'number') return value.toFixed(2);
    return String(value);
  }

  /** The latest decision's core signal (ENTER/EXIT/HOLD), when present. */
  signalOf(decision: Record<string, unknown> | null): string | null {
    const value = decision?.['signal'];
    return typeof value === 'string' ? value : null;
  }

  /** The instance's namespace-attributed owned positions as rows (#398). */
  brokerPositions(broker: InstanceBrokerView): { symbol: string; qty: number }[] {
    return Object.entries(broker.owned_positions).map(([symbol, qty]) => ({ symbol, qty }));
  }

  /** Account residual (unattributed) positions as rows (#399). */
  residualRows(fleet: FleetContamination): { symbol: string; qty: number }[] {
    return Object.entries(fleet.residual).map(([symbol, qty]) => ({ symbol, qty }));
  }
}
