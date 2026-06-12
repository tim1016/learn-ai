import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  resource,
  signal,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  DecisionColumnDescriptor,
  DesiredStateAction,
  FleetContamination,
  InstanceBrokerView,
  InstanceLastExit,
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
import { BrokerProvenanceCardComponent } from '../broker-provenance-card/broker-provenance-card.component';
import { BrokerRunLogModalComponent } from '../broker-run-log-modal/broker-run-log-modal.component';
import { type OperationError, type OperationKind, toOperationError } from '../operation-error';
import { BotTradeChartCardComponent } from './bot-trade-chart-card/bot-trade-chart-card.component';
import { BotTradesTableComponent } from './bot-trades-table/bot-trades-table.component';
import { BotFailuresTableComponent } from './bot-failures-table/bot-failures-table.component';

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
  /** 'hard' fails block trading; 'soft' fails are advisory (degrade, don't block). */
  severity: 'hard' | 'soft';
  detail: string;
  meaning: string;
  fix: string;
}

/** What the per-gate "Fix this" button does. `reconcile` fires the safe RECONCILE
 * command; `set-intent` scrolls to Bot Behavior; `view-log` opens the run-log
 * modal to inspect a halt; `reveal` just expands the written guidance when no
 * one-click remedy exists. */
type GateActionKind = 'reconcile' | 'set-intent' | 'view-log' | 'reveal';

interface GateAction {
  kind: GateActionKind;
  label: string;
  disabled?: boolean;
  /** Overrides `row.fix` as the button tooltip when the action is gated. */
  hint?: string;
}

/** The run a log view should target, plus whether it's still being written. */
interface LogTarget {
  runId: string;
  live: boolean;
}

// Plain-language stories for the engine's safety halt triggers (poisoned.flag),
// so "Safety halt" can say *what* the engine detected.
const HALT_TRIGGER_COPY: Record<string, string> = {
  outside_mutation:
    'A trade the bot did not place was seen on the account (a manual order, another bot, or a leftover) — it halted rather than trade against an unknown position.',
  lost_fill:
    'An order the bot placed never confirmed a fill within its window — it halted rather than act on an uncertain position.',
  cold_start_divergence:
    "On startup the bot couldn't reconcile its own records against the broker — it refused to resume rather than risk acting on stale state.",
  operator_declared: 'An operator manually flagged this run unsafe.',
};

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
    BrokerProvenanceCardComponent,
    BrokerRunLogModalComponent,
    BotTradeChartCardComponent,
    BotTradesTableComponent,
    BotFailuresTableComponent,
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
  readonly busyEmergencyFlatten = signal<boolean>(false);

  // Structured inline errors (handoff: inline-only surfacing, never a toast).
  readonly intentError = signal<OperationError | null>(null);
  readonly commandError = signal<OperationError | null>(null);
  readonly advancedOpen = signal<boolean>(false);
  readonly intentChoices = INTENT_CHOICES;
  readonly advancedActions = ADVANCED_ACTIONS;

  // Run-log modal: the run currently shown, or null when closed.
  readonly runLog = signal<LogTarget | null>(null);

  // Checklist "Fix this" state: which gate's written guidance is expanded, and
  // the busy/result of a one-click reconcile fix (kept separate from the
  // Advanced-card command error so each surface owns its own feedback).
  readonly expandedGate = signal<string | null>(null);
  readonly busyFixKey = signal<string | null>(null);
  readonly fixError = signal<OperationError | null>(null);
  readonly fixNotice = signal<string | null>(null);

  private readonly behaviorCard = viewChild<ElementRef<HTMLElement>>('behaviorCard');

  select(instanceId: string): void {
    this.selectedInstanceId.set(instanceId);
    this.lastActuation.set(null);
    this.intentError.set(null);
    this.commandError.set(null);
    this.runLog.set(null);
    this.expandedGate.set(null);
    this.fixError.set(null);
    this.fixNotice.set(null);
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
      const typed = window.prompt(
        'Flagging this instance as POISONED is IRREVERSIBLE: the current run can ' +
          'never resume on its run_id. Recovery requires a fresh deployment ' +
          '(new run_id) after you reconcile the account.\n\nType HALT to confirm.',
      );
      if (typed !== 'HALT') return;
    }
    await this.issueCommand(verb);
  }

  /** Account-wide emergency flatten (§ 7.2 #6) — independent of a live binding,
   * so it works after a halt/poison when the binding-gated FLATTEN command does
   * not. Places real (paper) market orders, so it double-confirms: an explicit
   * acknowledgement plus the account id echoed back (mirrors the CLI gate). */
  async issueEmergencyFlatten(): Promise<void> {
    const id = this.selectedInstanceId();
    if (id === null) return;
    const ok = window.confirm(
      'Emergency flatten places market orders to liquidate ALL positions on your ' +
        'account, regardless of which bot owns them. It is immediate and irreversible.\n\n' +
        'Continue?',
    );
    if (!ok) return;
    const account = window.prompt('Type your IBKR account id (e.g. DU1234567) to confirm:');
    if (account === null || account.trim() === '') return;
    this.busyEmergencyFlatten.set(true);
    this.commandError.set(null);
    try {
      await this.svc.emergencyFlattenAccount(id, {
        account: account.trim().toUpperCase(),
        confirm: true,
      });
      if (this.selectedInstanceId() === id) this.status.reload();
    } catch (err) {
      if (this.selectedInstanceId() === id) this.commandError.set(toOperationError('flatten', err));
    } finally {
      this.busyEmergencyFlatten.set(false);
    }
  }

  setAdvancedOpen(event: Event): void {
    if (event.target instanceof HTMLDetailsElement) {
      this.advancedOpen.set(event.target.open);
    }
  }

  /** True when the connected IBKR session is the paper account. Paper-only
   * surfaces (the Reset Paper Account button, the foreign-exec-replay
   * notice) appear only in this case — there is no "reset" on a live
   * account, and the warning would be misleading there. */
  isPaperAccount(): boolean {
    return this.connectivity.isPaper() === true;
  }

  /** Open IBKR's Account Management portal in a new tab so the operator can
   * trigger Paper Trading Account Reset from there. IBKR exposes no API
   * for this — the button is a deep-link + inline how-to, not a
   * server-side action. Documented as a workaround for the
   * connect-time execution-replay halt observed 2026-06-12. */
  openPaperAccountReset(): void {
    // The portal hostname differs by region; the public Account Management
    // landing page redirects to the right one after login, so we link there.
    const url = 'https://www.interactivebrokers.com/portal';
    window.open(url, '_blank', 'noopener,noreferrer');
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
    // Exit-reason FIRST. The hydration receipt can read accepted=false /
    // "missing" even on a healthy cold start (hydrate_policy=optional), so the
    // *reason the run ended* — not the receipt — must drive the headline.
    // Keying on the receipt first mis-reported a fatal_halt as a seed-day issue.

    // Clean / operator-initiated stops — informational, not alarming.
    if (
      !e.halt_trigger &&
      (e.exit_reason === 'normal' ||
        e.exit_reason === 'force_flat_complete' ||
        e.exit_reason === 'keyboard_interrupt' ||
        e.exit_reason === 'signal')
    ) {
      return {
        tone: 'ok',
        title: 'Last session ended cleanly',
        detail: `The previous run stopped without error${code != null ? ` (exit ${code})` : ''}.`,
        fix: 'Press Start Trading to begin a new session.',
      };
    }
    // Safety halt — the engine detected a problem mid-session and stopped to
    // protect the account. This can leave an OPEN position the bot is no
    // longer managing, so it's the highest-priority thing to surface.
    if (e.exit_reason === 'fatal_halt') {
      const trigger = this.haltTriggerStory(e);
      return {
        tone: 'bad',
        title: 'Safety halt — the bot stopped to protect the account',
        detail: trigger
          ? `${trigger} A position may still be open at the broker.`
          : `The engine hit a fatal halt mid-session${code != null ? ` (exit ${code})` : ''} — e.g. an order it could not reconcile. A position may still be open at the broker.`,
        fix: 'Check the broker account, reconcile, and flatten any position the bot is no longer tracking before restarting.',
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
    if (e.exit_reason === 'recovery_flatten') {
      return {
        tone: 'warn',
        title: 'Stopped and flattened on recovery',
        detail: 'The run hit an error and the recovery path flattened positions before exiting.',
        fix: 'Review the run log for the underlying error before restarting.',
      };
    }
    // Poisoned: the run refused to restart on its own run_id. Poison is sticky
    // and run_id-scoped by design — the only recovery is a fresh deployment.
    if (e.exit_reason === 'poisoned') {
      const trigger = this.haltTriggerStory(e);
      return {
        tone: 'bad',
        title: 'Run is poisoned — a fresh deployment is required',
        detail: trigger
          ? `${trigger} The same run can never resume on its run_id.`
          : `This run was flagged unsafe and refused to restart on its own run_id${code != null ? ` (exit ${code})` : ''}. The same run can never resume.`,
        fix: 'Reconcile the broker account, then Re-deploy below to start a fresh run_id.',
      };
    }
    // Cold-start / seed-day: only reached for non-halt exits (e.g. an exit-4
    // hydration rejection under hydrate_policy=require with no prior state).
    if (e.hydration_accepted === false && e.hydration_failure_reason === 'missing') {
      return {
        tone: 'warn',
        title: 'Needs a seed day (no saved indicator state)',
        detail: 'The run had no saved indicator state to resume from — expected on a first run.',
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
    // A run that left a halt_trigger but exited via an otherwise-clean path
    // (an operator MARK_POISONED writes poisoned.flag and then stops as
    // keyboard_interrupt/signal). The clean-exit branch above already excluded
    // these, so surface the trigger here rather than letting it fall through to
    // the generic "ended unexpectedly" notice.
    const trigger = this.haltTriggerStory(e);
    if (trigger) {
      return {
        tone: 'bad',
        title: 'Run flagged unsafe — a fresh deployment is required',
        detail: `${trigger} The same run can never resume on its run_id.`,
        fix: 'Reconcile the broker account, then Re-deploy below to start a fresh run_id.',
      };
    }
    // Generic failure (exception / unknown, with no hydration cause).
    return {
      tone: 'bad',
      title: 'Last session ended unexpectedly',
      detail: `The previous run exited${code != null ? ` with code ${code}` : ''}${e.exit_reason ? ` (${e.exit_reason})` : ''}.`,
      fix: 'Open the run log for the full detail.',
    };
  }

  /** Plain-language story for the engine's safety halt trigger (poisoned.flag),
   * or '' when the run left no trigger. */
  private haltTriggerStory(e: InstanceLastExit): string {
    if (!e.halt_trigger) return '';
    return HALT_TRIGGER_COPY[e.halt_trigger] ?? `Safety trigger: ${e.halt_trigger}.`;
  }

  checklistRows(r: ReadinessVector | null): ChecklistRow[] {
    if (!r) {
      return [
        {
          key: 'readiness',
          label: 'Pre-Trade Status Loaded',
          status: 'unknown',
          severity: 'soft',
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
      severity: gate.severity,
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

  /** True when a STOPPED instance can be recovered by re-deploying a fresh
   * run_id — the only recovery path for a poisoned/halted run. Requires the
   * bound run's ledger deploy identity (start_defaults) to prefill the form. */
  canRedeploy(s: LiveInstanceStatus): boolean {
    if (s.process.state === 'running' || s.process.state === 'stopping') return false;
    return !!s.start_defaults?.strategy_spec_path;
  }

  /** Deep-link query params that prefill the deploy form from the bound run's
   * ledger, so re-deploying (fresh run_id) doesn't make the operator re-type the
   * deploy identity. */
  redeployQueryParams(s: LiveInstanceStatus): Record<string, string> {
    const d = s.start_defaults;
    if (!d) return {};
    return {
      strategy_key: d.strategy ?? '',
      spec_path: d.strategy_spec_path ?? '',
      account_id: d.account_id ?? '',
      qc_backtest_id: d.qc_cloud_backtest_id ?? '',
      qc_audit_copy_path: d.qc_audit_copy_path ?? '',
      instance_id: s.strategy_instance_id,
    };
  }

  /** Which run a log view should open for this instance: the live run when one
   * is bound (still being written), else the run that just exited, else the
   * ledger's latest evidence run. Null when nothing has ever run. */
  logTarget(s: LiveInstanceStatus): LogTarget | null {
    if (s.live_binding) return { runId: s.live_binding.run_id, live: true };
    if (s.last_exit) return { runId: s.last_exit.run_id, live: false };
    if (s.evidence_binding) return { runId: s.evidence_binding.run_id, live: false };
    return null;
  }

  openRunLog(target: LogTarget): void {
    this.runLog.set(target);
  }

  closeRunLog(): void {
    this.runLog.set(null);
  }

  /** What "Fix this" does for a given gate. Resolved against live status because
   * the right remedy depends on whether the bot is running (reconcile routes to
   * a live binding) and whether a run log exists to inspect. */
  fixAction(row: ChecklistRow, s: LiveInstanceStatus): GateAction {
    switch (row.key) {
      case 'latest_reconcile':
        // RECONCILE is a command on the live binding — it can't run against a
        // stopped bot, so fall back to revealing the start-first guidance.
        if (!s.live_binding) {
          return {
            kind: 'reveal',
            label: 'How to fix',
            hint: 'Start the bot first — re-sync runs against the live session.',
          };
        }
        return {
          kind: 'reconcile',
          label: this.busyFixKey() === row.key ? 'Re-syncing…' : 'Re-sync now',
          disabled: this.busyFixKey() !== null,
        };
      case 'desired_state':
        return { kind: 'set-intent', label: 'Set bot intent' };
      case 'poison_sentinel':
      case 'prior_day_halt':
        // Reviewing the run log is the actual first step for a halt / carried-
        // over issue. Only offer it when there's a run to read.
        return this.logTarget(s)
          ? { kind: 'view-log', label: 'View run log' }
          : { kind: 'reveal', label: 'How to fix' };
      default:
        return { kind: 'reveal', label: 'How to fix' };
    }
  }

  /** Dispatch the gate's "Fix this" action. */
  runFix(row: ChecklistRow, s: LiveInstanceStatus): void {
    const action = this.fixAction(row, s);
    switch (action.kind) {
      case 'reconcile':
        void this.runReconcileFix();
        return;
      case 'set-intent':
        this.scrollToBehavior();
        return;
      case 'view-log': {
        const target = this.logTarget(s);
        if (target) this.openRunLog(target);
        else this.toggleGuidance(row.key);
        return;
      }
      case 'reveal':
        this.toggleGuidance(row.key);
        return;
    }
  }

  /** One-click remedy for the Account-Reconciled gate: issue RECONCILE to the
   * bound run and report the outcome inline on the checklist (not in the
   * Advanced card, where the operator who clicked "Re-sync now" wouldn't see it). */
  private async runReconcileFix(): Promise<void> {
    const id = this.selectedInstanceId();
    if (id === null) return;
    this.busyFixKey.set('latest_reconcile');
    this.fixError.set(null);
    this.fixNotice.set(null);
    try {
      await this.svc.issueInstanceCommand(id, { verb: 'RECONCILE' });
      if (this.selectedInstanceId() === id) {
        this.fixNotice.set('Re-sync requested — the bot refreshes account balances on its next loop.');
        this.commands.reload();
        this.status.reload();
      }
    } catch (err) {
      if (this.selectedInstanceId() === id) this.fixError.set(toOperationError('reconcile', err));
    } finally {
      this.busyFixKey.set(null);
    }
  }

  /** Bring the Bot Behavior card into view and move focus to the card itself
   * (not its first segment — that would nudge the operator toward Pause and
   * no-op while a save is in flight and the buttons are disabled). */
  private scrollToBehavior(): void {
    const el = this.behaviorCard()?.nativeElement;
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.focus({ preventScroll: true });
  }

  toggleGuidance(key: string): void {
    this.expandedGate.update((current) => (current === key ? null : key));
  }
}
