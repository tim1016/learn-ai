import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';
import type { HydratePolicy } from '../../../api/live-runs.types';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';

// A legacy ledger has no recorded strategy_key, so start_defaults.strategy is
// empty. Fall back to the historical default so the form is always runnable;
// the backend foot-gun guard only protects ledgers that DID record a key.
const FALLBACK_STRATEGY = 'spy_ema_crossover';

/**
 * Start/Stop card for the instance console (#416). Restores the only Start
 * affordance after the console cutover (#410) retired the old paper-run page.
 *
 * The five `run start` knobs are defaulted from the selected instance's ledger
 * (`start_defaults`, server-authored) rather than blank/hardcoded constants —
 * closing the foot-gun where a mismatched `strategy` silently runs a different
 * algorithm than the ledger reconciled to. All messaging routes through the
 * connectivity strip + operation-error pattern: a control that can't act is
 * disabled with a visible, specific reason, never a bare greyed button.
 */
@Component({
  selector: 'app-broker-start-stop-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BrokerOperationResultComponent],
  templateUrl: './broker-start-stop-card.component.html',
  styleUrl: './broker-start-stop-card.component.scss',
})
export class BrokerStartStopCardComponent {
  private readonly svc = inject(LiveRunsService);
  protected readonly connectivity = inject(BrokerConnectivityService);

  readonly status = input.required<LiveInstanceStatus>();
  /** Emitted after a start/stop the daemon accepted, so the parent reloads. */
  readonly changed = output();

  // Editable form fields, re-seeded from the server-authored defaults whenever
  // a different instance is selected (linkedSignal resets on source change).
  readonly strategy = linkedSignal<string>(
    () => this.status().start_defaults?.strategy || FALLBACK_STRATEGY,
  );
  readonly readonlyFlag = linkedSignal<boolean>(
    () => this.status().start_defaults?.readonly ?? true,
  );
  readonly hydratePolicy = linkedSignal<HydratePolicy>(
    () => this.status().start_defaults?.hydrate_policy ?? 'require',
  );
  readonly maxOrdersPerDay = linkedSignal<number>(
    () => this.status().start_defaults?.max_orders_per_day ?? 50_000,
  );
  readonly ibkrHost = linkedSignal<string>(
    () => this.status().start_defaults?.ibkr_host ?? '127.0.0.1',
  );

  readonly busy = signal<boolean>(false);
  readonly result = signal<OperationError | null>(null);

  /** The run a start/stop targets: the live run if bound, else the latest run. */
  readonly runId = computed<string | null>(() => {
    const s = this.status();
    return s.live_binding?.run_id ?? s.evidence_binding?.run_id ?? null;
  });

  private readonly processState = computed(() => this.status().process.state);
  readonly isLive = computed<boolean>(() => {
    const state = this.processState();
    return state === 'running' || state === 'stopping';
  });

  /** Why Start can't be clicked, sourced from the connectivity-strip signals.
   * Null = Start is available. */
  readonly startBlockedReason = computed<string | null>(() => {
    if (this.isLive()) return null; // running → the Stop control is the relevant one
    if (this.runId() === null) return 'No run to start — deploy a run first.';
    if (this.connectivity.daemonDown()) {
      return 'Host daemon unreachable — start the host daemon to launch a run.';
    }
    // Fleet safety policy (fleet_dirty_blocks_starts): when the account is
    // contaminated and policy blocks starts, the strip already says so — the
    // Start control must honour it too, not post to the daemon behind the
    // policy's back.
    if (this.connectivity.fleetBlocksStarts()) {
      return 'Fleet policy blocks new starts — the account is contaminated.';
    }
    return null;
  });

  readonly canStart = computed<boolean>(
    () => !this.busy() && !this.isLive() && this.startBlockedReason() === null,
  );
  readonly canStop = computed<boolean>(() => !this.busy() && this.isLive() && this.runId() !== null);

  async start(): Promise<void> {
    const runId = this.runId();
    if (runId === null) return;
    this.busy.set(true);
    this.result.set(null);
    try {
      await this.svc.startHostRunner(runId, {
        readonly: this.readonlyFlag(),
        hydrate_policy: this.hydratePolicy(),
        strategy: this.strategy(),
        max_orders_per_day: this.maxOrdersPerDay(),
        ibkr_host: this.ibkrHost(),
      });
      this.changed.emit();
    } catch (err) {
      this.result.set(toOperationError('start', err));
    } finally {
      this.busy.set(false);
    }
  }

  async stop(): Promise<void> {
    const runId = this.runId();
    if (runId === null) return;
    const ok = window.confirm('Are you sure? This will stop all trading activity.');
    if (!ok) return;
    this.busy.set(true);
    this.result.set(null);
    try {
      await this.svc.stopHostRunner(runId, { force: false });
      this.changed.emit();
    } catch (err) {
      this.result.set(toOperationError('stop', err));
    } finally {
      this.busy.set(false);
    }
  }

  // Event readers that narrow without a type assertion.
  setStrategy(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.strategy.set(e.target.value);
  }
  setReadonly(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.readonlyFlag.set(e.target.checked);
  }
  setHydratePolicy(e: Event): void {
    if (!(e.target instanceof HTMLSelectElement)) return;
    const v = e.target.value;
    if (v === 'require' || v === 'optional' || v === 'disabled') this.hydratePolicy.set(v);
  }
  setMaxOrdersPerDay(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.maxOrdersPerDay.set(e.target.valueAsNumber);
  }
  setIbkrHost(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.ibkrHost.set(e.target.value);
  }
}
