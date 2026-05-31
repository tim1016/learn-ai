import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import type {
  DecisionColumnDescriptor,
  DesiredStateAction,
  InstanceBrokerView,
  IntentActuation,
  LiveInstanceSummary,
} from '../../../api/live-instances.types';
import type { CommandEntry, CommandVerb } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';

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
  templateUrl: './broker-instances.component.html',
  styleUrl: './broker-instances.component.scss',
})
export class BrokerInstancesComponent {
  private readonly svc = inject(LiveRunsService);

  readonly selectedInstanceId = signal<string | null>(null);

  readonly fleet = resource({
    loader: () => this.svc.getInstances(),
  });

  readonly status = resource({
    params: () => this.selectedInstanceId() ?? undefined,
    loader: ({ params }) => this.svc.getInstanceStatus(params),
  });

  readonly instances = computed<LiveInstanceSummary[]>(() => this.fleet.value() ?? []);

  readonly commands = resource({
    params: () => this.selectedInstanceId() ?? undefined,
    loader: ({ params }) => this.svc.getInstanceCommands(params),
  });
  readonly commandEntries = computed<CommandEntry[]>(() => this.commands.value()?.entries ?? []);

  readonly busyAction = signal<DesiredStateAction | null>(null);
  readonly lastActuation = signal<IntentActuation | null>(null);
  readonly busyVerb = signal<CommandVerb | null>(null);

  select(instanceId: string): void {
    this.selectedInstanceId.set(instanceId);
    this.lastActuation.set(null);
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
    try {
      const result = await this.svc.setInstanceDesiredState(id, { action });
      if (this.selectedInstanceId() === id) {
        this.lastActuation.set(result.actuation);
        this.status.reload();
      }
    } finally {
      this.busyAction.set(null);
    }
  }

  /** Issue a one-shot command (FLATTEN/RECONCILE/MARK_POISONED) to the bound run (#397). */
  async issueCommand(verb: CommandVerb): Promise<void> {
    const id = this.selectedInstanceId();
    if (id === null) return;
    this.busyVerb.set(verb);
    try {
      await this.svc.issueInstanceCommand(id, { verb });
      if (this.selectedInstanceId() === id) this.commands.reload();
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
}
