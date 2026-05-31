import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
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
  SpecStrategyFixture,
} from '../../../api/live-runs.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';

/**
 * Deploy form for a live strategy instance. The UI uses plain operator words,
 * while the request still maps exactly to ADR 0006: create the run on the host,
 * bind it to a QC backtest receipt, and optionally start it immediately.
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
  readonly specFixtures = resource({ loader: () => this.svc.getSpecStrategyFixtures() });
  readonly qcCopies = resource({ loader: () => this.svc.getQcAuditCopies() });
  // Best-effort: the account prefill is convenience only — broker may be down,
  // in which case the operator types the account id manually.
  readonly account = resource({ loader: () => this.broker.account() });

  // Form fields.
  readonly strategyKey = signal<string>('');
  readonly specPath = signal<string>('');
  readonly manualSpecPath = signal<boolean>(false);
  readonly accountId = linkedSignal<string>(() => this.account.value()?.account_id ?? '');
  readonly qcBacktestId = signal<string>('');
  readonly qcAuditCopyPath = signal<string>('');
  readonly instanceId = signal<string>('');
  readonly readonlyFlag = signal<boolean>(true);
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = signal<number>(4);
  readonly startNow = signal<boolean>(false);
  readonly showLiveConfirm = signal<boolean>(false);
  private readonly liveConfirmed = signal<boolean>(false);

  readonly busy = signal<boolean>(false);
  readonly error = signal<OperationError | null>(null);
  readonly deployed = signal<HostRunnerDeployResponse | null>(null);

  // Captured once when the form opens, NOT per-submit: start_date_ms is part of
  // the content-addressed run_id hash, so a retry with identical inputs must
  // reuse the same value to hit the backend's idempotent no-op (created=false)
  // rather than minting a new run_id off the current clock.
  private readonly startDateMs = Date.now();

  readonly selectedFixture = computed<SpecStrategyFixture | null>(
    () => this.specFixtures.value()?.find((f) => f.path === this.specPath()) ?? null,
  );

  readonly launchMode = computed<'paper' | 'live'>(() => (this.readonlyFlag() ? 'paper' : 'live'));

  readonly fieldsReady = computed<boolean>(() => this.required());

  readonly nowChecks = computed(() => [
    {
      key: 'engine',
      label: 'Engine up',
      state: this.connectivity.daemonState(),
      detail:
        this.connectivity.daemonState() === 'ok'
          ? 'Ready'
          : this.connectivity.daemonState() === 'unknown'
            ? 'Checking'
            : 'Start it, then recheck',
    },
    {
      key: 'broker',
      label: 'Broker',
      state: this.connectivity.brokerState(),
      detail:
        this.connectivity.brokerState() === 'ok'
          ? 'Connected'
          : this.connectivity.brokerState() === 'unknown'
            ? 'Checking'
            : 'Disconnected',
    },
    {
      key: 'fields',
      label: 'Fields',
      state: this.fieldsReady() ? 'ok' : 'warn',
      detail: this.fieldsReady() ? 'Complete' : 'Required fields missing',
    },
    {
      key: 'fleet',
      label: 'Fleet clear',
      state: this.connectivity.fleetState(),
      detail:
        this.connectivity.fleetState() === 'warn'
          ? 'Starts blocked'
          : this.connectivity.fleetState() === 'unknown'
            ? this.connectivity.nothingDeployed()
              ? 'Nothing deployed'
              : 'Checking'
            : 'Clear',
    },
  ]);

  readonly deployChecks = computed(() => [
    {
      key: 'tree',
      label: 'Working tree clean',
      state: this.error()?.status === 409 ? 'down' : 'pending',
      detail: this.error()?.status === 409
        ? 'Commit or stash the listed files'
        : 'Checked when you deploy',
    },
    {
      key: 'spec',
      label: 'Spec matches strategy',
      state: this.error()?.status === 400 ? 'down' : 'pending',
      detail: this.error()?.status === 400
        ? 'Pick the matching spec'
        : 'Checked when you deploy',
    },
  ]);

  constructor() {
    effect(() => {
      if (this.manualSpecPath()) return;
      const strategy = this.strategyKey();
      const fixtures = this.specFixtures.value() ?? [];
      const match = fixtures.find((f) => f.name === strategy);
      if (match && this.specPath() !== match.path) {
        this.specPath.set(match.path);
      }
    });
  }

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
      return 'Live engine unavailable. Start it on this machine, then recheck.';
    }
    if (this.startNow() && this.connectivity.fleetBlocksStarts()) {
      return 'Fleet state blocks new starts. Turn off "Start trading immediately" to deploy only, or clear the account state.';
    }
    if (!this.required()) return 'Fill every required field before deploying.';
    return null;
  });

  readonly canSubmit = computed<boolean>(() => !this.busy() && this.blockedReason() === null);

  async submit(): Promise<void> {
    if (!this.canSubmit()) return;
    if (this.startNow() && !this.readonlyFlag() && !this.liveConfirmed()) {
      this.showLiveConfirm.set(true);
      return;
    }
    this.liveConfirmed.set(false);
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

  async confirmLiveAndSubmit(): Promise<void> {
    this.showLiveConfirm.set(false);
    this.liveConfirmed.set(true);
    await this.submit();
  }

  cancelLiveConfirm(): void {
    this.showLiveConfirm.set(false);
    this.liveConfirmed.set(false);
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
  setSpecFixturePath(e: Event): void {
    this.manualSpecPath.set(false);
    this.specPath.set(this.text(e));
  }
  useManualSpecPath(): void {
    this.manualSpecPath.set(true);
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
    if (e.target instanceof HTMLInputElement) {
      this.readonlyFlag.set(e.target.value !== 'live');
      this.liveConfirmed.set(false);
    }
  }
  setHydratePolicy(e: Event): void {
    const v = this.text(e);
    if (v === 'require' || v === 'optional' || v === 'disabled') this.hydratePolicy.set(v);
  }
  setMaxOrders(e: Event): void {
    if (e.target instanceof HTMLInputElement) this.maxOrdersPerDay.set(e.target.valueAsNumber);
  }
  setStartNow(e: Event): void {
    if (e.target instanceof HTMLInputElement) {
      this.startNow.set(e.target.checked);
      this.liveConfirmed.set(false);
    }
  }
}
