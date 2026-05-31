import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import type {
  HostRunnerDeployRequest,
  HostRunnerDeployResponse,
  HydratePolicy,
} from '../../../api/live-runs.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';

/**
 * Deploy form — stage 1 of the deploy pipeline (ADR 0006), the create-a-run UI
 * the platform never had. Lifts the `init-ledger` CLI args into a form and
 * forwards to the host daemon via `POST /api/live-instances`.
 *
 * The QC anchor (backtest id + committed audit copy) is mandatory by design —
 * deploy is reconciliation-gated, not one-click. v1 sources both by manual entry
 * (ADR 0006 §4): the operator types the backtest id and picks an audit copy that
 * is already committed under `references/qc-shadow/` (the clean-tree check
 * enforces this). The algorithm is chosen from the engine registry; its `name`
 * pins both the spec-reconciled `strategy_key` and the launch `strategy`.
 *
 * All messaging routes through the connectivity strip + operation-error pattern;
 * the dirty-tree precondition (the most likely operator confusion) surfaces its
 * offending paths via the backend detail line.
 */
@Component({
  selector: 'app-broker-deploy-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, BrokerConnectivityStripComponent, BrokerOperationResultComponent],
  templateUrl: './broker-deploy-form.component.html',
  styleUrl: './broker-deploy-form.component.scss',
})
export class BrokerDeployFormComponent {
  private readonly svc = inject(LiveRunsService);
  private readonly broker = inject(BrokerService);
  protected readonly connectivity = inject(BrokerConnectivityService);

  readonly strategies = resource({ loader: () => this.svc.getEngineStrategies() });
  readonly qcCopies = resource({ loader: () => this.svc.getQcAuditCopies() });
  // Best-effort: the account prefill is convenience only — broker may be down,
  // in which case the operator types the account id manually.
  readonly account = resource({ loader: () => this.broker.account() });

  // Form fields.
  readonly strategyKey = signal<string>('');
  readonly specPath = signal<string>('');
  readonly accountId = linkedSignal<string>(() => this.account.value()?.account_id ?? '');
  readonly qcBacktestId = signal<string>('');
  readonly qcAuditCopyPath = signal<string>('');
  readonly instanceId = signal<string>('');
  readonly readonlyFlag = signal<boolean>(true);
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = signal<number>(4);
  readonly startNow = signal<boolean>(false);

  readonly busy = signal<boolean>(false);
  readonly error = signal<OperationError | null>(null);
  readonly deployed = signal<HostRunnerDeployResponse | null>(null);

  // Captured once when the form opens, NOT per-submit: start_date_ms is part of
  // the content-addressed run_id hash, so a retry with identical inputs must
  // reuse the same value to hit the backend's idempotent no-op (created=false)
  // rather than minting a new run_id off the current clock.
  private readonly startDateMs = Date.now();

  private readonly required = computed<boolean>(
    () =>
      this.strategyKey().trim() !== '' &&
      this.specPath().trim() !== '' &&
      this.accountId().trim() !== '' &&
      this.qcBacktestId().trim() !== '' &&
      this.qcAuditCopyPath().trim() !== '' &&
      this.instanceId().trim() !== '',
  );

  /** Why Deploy can't be submitted, sourced from the connectivity strip + form.
   * Null = ready. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.connectivity.daemonDown()) {
      return 'Host daemon unreachable — deploy runs git operations on the host; start the daemon and retry.';
    }
    if (this.startNow() && this.connectivity.fleetBlocksStarts()) {
      return 'Fleet policy blocks new starts — uncheck "Start now" to deploy without launching, or resolve the contamination.';
    }
    if (!this.required()) return 'Fill every required field before deploying.';
    return null;
  });

  readonly canSubmit = computed<boolean>(() => !this.busy() && this.blockedReason() === null);

  async submit(): Promise<void> {
    if (!this.canSubmit()) return;
    this.busy.set(true);
    this.error.set(null);
    this.deployed.set(null);
    const strategyKey = this.strategyKey().trim();
    const request: HostRunnerDeployRequest = {
      strategy_spec_path: this.specPath().trim(),
      qc_audit_copy_path: this.qcAuditCopyPath().trim(),
      qc_cloud_backtest_id: this.qcBacktestId().trim(),
      account_id: this.accountId().trim(),
      start_date_ms: this.startDateMs,
      strategy_instance_id: this.instanceId().trim(),
      strategy_key: strategyKey,
      start: this.startNow(),
    };
    // Only attach launch knobs when actually starting — otherwise a deploy-only
    // request carries irrelevant start_options that still get validated (and a
    // cleared "max orders" field would serialize NaN → null and fail).
    if (this.startNow()) {
      const maxOrders = this.maxOrdersPerDay();
      request.start_options = {
        readonly: this.readonlyFlag(),
        hydrate_policy: this.hydratePolicy(),
        strategy: strategyKey,
        max_orders_per_day: Number.isFinite(maxOrders) ? maxOrders : 4,
        ibkr_host: '127.0.0.1',
      };
    }
    try {
      const response = await this.svc.deployInstance(request);
      this.deployed.set(response);
    } catch (err) {
      this.error.set(toOperationError('deploy', err));
    } finally {
      this.busy.set(false);
    }
  }

  // Event readers that narrow without a type assertion.
  private text(e: Event): string {
    return e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement
      ? e.target.value
      : '';
  }
  setStrategyKey(e: Event): void {
    this.strategyKey.set(this.text(e));
  }
  setSpecPath(e: Event): void {
    this.specPath.set(this.text(e));
  }
  setAccountId(e: Event): void {
    this.accountId.set(this.text(e));
  }
  setQcBacktestId(e: Event): void {
    this.qcBacktestId.set(this.text(e));
  }
  setQcAuditCopyPath(e: Event): void {
    this.qcAuditCopyPath.set(this.text(e));
  }
  setInstanceId(e: Event): void {
    this.instanceId.set(this.text(e));
  }
  setReadonly(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.readonlyFlag.set(e.target.checked);
  }
  setHydratePolicy(e: Event): void {
    const v = this.text(e);
    if (v === 'require' || v === 'optional' || v === 'disabled') this.hydratePolicy.set(v);
  }
  setMaxOrders(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.maxOrdersPerDay.set(e.target.valueAsNumber);
  }
  setStartNow(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.startNow.set(e.target.checked);
  }
}
