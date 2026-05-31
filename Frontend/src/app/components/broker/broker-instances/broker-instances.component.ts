import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  DecisionColumnDescriptor,
  DesiredStateAction,
  FleetContamination,
  InstanceBrokerView,
  IntentActuation,
  LiveInstanceSummary,
} from '../../../api/live-instances.types';
import type { CommandEntry, CommandVerb } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { BrokerStartStopCardComponent } from '../broker-start-stop-card/broker-start-stop-card.component';
import { type OperationError, type OperationKind, toOperationError } from '../operation-error';

// One-shot command verb -> operation kind for the error map.
const VERB_TO_KIND: Record<CommandVerb, OperationKind> = {
  RECONCILE: 'reconcile',
  FLATTEN: 'flatten',
  MARK_POISONED: 'mark-poisoned',
  PAUSE: 'pause',
  RESUME: 'resume',
  STOP: 'stop',
};

/**
 * Instance control room — foundation (ADR 0004).
 *
 * The console's subject is the strategy instance; the current run and its
 * artifacts are attached as evidence. This minimal view stands up the
 * instance spine (fleet list -> instance status with live-vs-evidence binding);
 * the full re-spine and operator panels land with the cutover slice.
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
  readonly accountContaminated = computed<boolean>(
    () => this.account.value()?.verdict === 'contaminated',
  );

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
