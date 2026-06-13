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
import { ActivatedRoute, RouterLink } from '@angular/router';
import type {
  HostRunnerDeployRequest,
  HostRunnerDeployResponse,
  HydratePolicy,
  SizingPolicy,
  SizingPreset,
  SpecStrategyFixture,
} from '../../../api/live-runs.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';

// Kept in lockstep with the backend guard `identity._INSTANCE_ID_RE`
// (and `live_instances._INSTANCE_ID_RE`): a deployment name the operate
// endpoints reject (e.g. one with a space) must be caught here too, so the
// operator sees the reason inline instead of a created-but-unusable instance.
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

const DEPLOYMENT_VALIDATION_AUDIT_COPY = 'references/qc-shadow/DeploymentValidationAlgorithm.py';
const DEPLOYMENT_VALIDATION_SPEC_PATH =
  'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json';

// ADR 0009 § 3 — Reference parity preset's policy. Pinned here as a constant
// so the gate lookup and the submit path use the *same* shape; a future change
// to the preset's all-in fraction needs to land in exactly one place.
const REFERENCE_PARITY_POLICY: SizingPolicy = { kind: 'SetHoldings', fraction: '1.0' };

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
  private readonly route = inject(ActivatedRoute);

  readonly strategies = resource({ loader: () => this.svc.getEngineStrategies() });
  readonly specFixtures = resource({ loader: () => this.svc.getSpecStrategyFixtures() });
  readonly qcCopies = resource({ loader: () => this.svc.getQcAuditCopies() });
  // Used only to pre-empt the daemon's "already active" 409: a start-immediately
  // deploy onto an instance that already has a live runner is rejected.
  readonly instances = resource({ loader: () => this.svc.getInstances() });
  // Best-effort: the account prefill is convenience only — broker may be down,
  // in which case the operator types the account id manually.
  readonly account = resource({ loader: () => this.broker.account() });

  // Form fields.
  readonly strategyKey = signal<string>('');
  readonly specPath = signal<string>('');
  readonly manualSpecPath = signal<boolean>(false);
  // Seeded from a re-deploy deep-link (recover a poisoned/halted instance with a
  // fresh run_id). Preferred over the broker-account prefill so the ledger's
  // account survives even if the broker probe is down or resolves late.
  private readonly seededAccountId = signal<string>('');
  readonly accountId = linkedSignal<string>(
    () => this.seededAccountId() || (this.account.value()?.account_id ?? ''),
  );
  readonly qcBacktestId = signal<string>('');
  readonly qcAuditCopyPath = signal<string>('');
  readonly instanceId = signal<string>('');
  readonly readonlyFlag = signal<boolean>(true);
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = signal<number>(50_000);
  readonly startNow = signal<boolean>(false);
  // ADR 0009 § 7 — position-sizing preset. Defaults to Safe canary
  // (FixedShares(1)); the $250k surprise from the first deployment-validation
  // run is opt-in. Reference parity is gated by the audit-copy allow-list
  // (ADR § 3); Custom ships in PR4.
  readonly sizingPreset = signal<SizingPreset>('safe_canary');

  // ADR 0009 § 3 — Reference parity gate. Refetched whenever the chosen audit
  // copy changes; surfaces the verdict (`proven_match` / `proven_mismatch` /
  // `cannot_prove`) inline so the operator sees *why* Reference parity is
  // available or not before clicking. **Crucially**, the lookup passes the
  // actual Reference parity policy (`SetHoldings(1.0)`) so the backend
  // compares the registered rule against the preset the operator would
  // submit; an audit copy registered as `SetHoldings(0.5)` would otherwise
  // pass the bare informational lookup and silently enable a Reference
  // parity click that submits `SetHoldings(1.0)`.
  readonly referenceParityGate = resource({
    params: () => ({ auditCopyPath: this.qcAuditCopyPath().trim() }),
    loader: async ({ params }) => {
      if (!params.auditCopyPath) return null;
      return this.svc.getAuditCopySizingLookup(
        params.auditCopyPath,
        REFERENCE_PARITY_POLICY,
      );
    },
    defaultValue: null,
  });

  readonly referenceParityAvailable = computed<boolean>(() => {
    const gate = this.referenceParityGate.value();
    return gate?.verdict === 'proven_match';
  });

  readonly referenceParityBanner = computed<string>(() => {
    const gate = this.referenceParityGate.value();
    if (!gate) return 'Pick an audit copy to check Reference parity availability.';
    return gate.detail;
  });

  // PR4 — Custom expansion. The operator picks a kind (FixedShares or
  // FixedNotional) and a value. The kind dropdown is the canonical name; the
  // value field accepts plain numbers (FixedShares) or decimal-string-friendly
  // numbers (FixedNotional). Decimal-on-the-wire is enforced at submit time so
  // the operator never sees a float at the API boundary.
  readonly customKind = signal<'FixedShares' | 'FixedNotional'>('FixedShares');
  readonly customValue = signal<string>('1');
  readonly showLiveConfirm = signal<boolean>(false);
  private readonly liveConfirmed = signal<boolean>(false);
  private readonly autoSelectedDeploymentValidationAuditCopy = signal<boolean>(false);

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

  readonly qcAuditCopyOptions = computed<string[]>(() => {
    const entries = this.qcCopies.value()?.entries ?? [];
    if (entries.includes(DEPLOYMENT_VALIDATION_AUDIT_COPY)) return entries;
    return [DEPLOYMENT_VALIDATION_AUDIT_COPY, ...entries];
  });

  readonly launchMode = computed<'paper' | 'live'>(() => (this.readonlyFlag() ? 'paper' : 'live'));

  /** True when the typed deployment name already has a live host runner. A
   * start-immediately deploy onto it would hit the daemon's 409 "Host runner
   * already active for … (instance …)"; deploy-only is unaffected. Matches the
   * backend's live set (running | stopping). */
  readonly instanceAlreadyRunning = computed<boolean>(() => {
    const id = this.instanceId().trim();
    if (id === '') return false;
    const match = this.instances.value()?.find((i) => i.strategy_instance_id === id);
    return match?.process_state === 'running' || match?.process_state === 'stopping';
  });

  /** True when a deployment name is typed but is not a valid single-segment id
   * (e.g. contains a space). Empty is "missing", not "invalid", so the
   * missing-fields message handles it. */
  readonly instanceIdInvalid = computed<boolean>(() => {
    const id = this.instanceId().trim();
    return id !== '' && !INSTANCE_ID_RE.test(id);
  });

  readonly fieldsReady = computed<boolean>(() => this.required());
  readonly missingRequiredFields = computed<string[]>(() => {
    const missing: string[] = [];
    if (this.strategyKey().trim() === '') missing.push('Strategy');
    if (this.specPath().trim() === '') missing.push('Strategy settings file');
    if (this.accountId().trim() === '') missing.push('Brokerage account');
    if (this.qcBacktestId().trim() === '') missing.push('Backtest ID');
    if (this.qcAuditCopyPath().trim() === '') missing.push('Algorithm audit copy');
    if (this.instanceId().trim() === '') missing.push('Deployment name');
    return missing;
  });

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
    // Re-deploy prefill: seed the form from the deep-link query params the
    // instance console builds from the bound run's ledger, so recovering a
    // poisoned/halted instance (which needs a fresh run_id) doesn't make the
    // operator re-type the deploy identity. Every field stays operator-editable.
    const qp = this.route.snapshot.queryParamMap;
    const seedStrategy = qp.get('strategy_key');
    if (seedStrategy) this.strategyKey.set(seedStrategy);
    const seedSpecPath = qp.get('spec_path');
    if (seedSpecPath) {
      // manual=true so the strategy→spec effect below doesn't override the
      // exact spec path the prior run was reconciled to.
      this.manualSpecPath.set(true);
      this.specPath.set(seedSpecPath);
    }
    const seedAccount = qp.get('account_id');
    if (seedAccount) this.seededAccountId.set(seedAccount);
    const seedBacktestId = qp.get('qc_backtest_id');
    if (seedBacktestId) this.qcBacktestId.set(seedBacktestId);
    const seedAuditCopy = qp.get('qc_audit_copy_path');
    if (seedAuditCopy) this.qcAuditCopyPath.set(seedAuditCopy);
    const seedInstanceId = qp.get('instance_id');
    if (seedInstanceId) this.instanceId.set(seedInstanceId);

    effect(() => {
      if (this.manualSpecPath()) return;
      const strategy = this.strategyKey();
      const fixtures = this.specFixtures.value() ?? [];
      const match = fixtures.find((f) => f.name === strategy);
      const fallback =
        strategy === 'deployment_validation' ? DEPLOYMENT_VALIDATION_SPEC_PATH : null;
      const nextPath = match?.path ?? fallback;
      if (nextPath && this.specPath() !== nextPath) this.specPath.set(nextPath);
    });
    // Reference parity must not silently downgrade — if the audit-copy choice
    // changes such that the gate is no longer proven_match, reset the preset to
    // Safe canary so the operator re-confirms the choice (ADR 0009 § 3 "the
    // preset's name is a promise, breaking it silently is bad audit UX").
    effect(() => {
      if (this.sizingPreset() === 'reference_parity' && !this.referenceParityAvailable()) {
        this.sizingPreset.set('safe_canary');
      }
    });

    effect(() => {
      if (this.strategyKey() === 'deployment_validation') {
        if (this.qcAuditCopyPath().trim() === '') {
          this.qcAuditCopyPath.set(DEPLOYMENT_VALIDATION_AUDIT_COPY);
          this.autoSelectedDeploymentValidationAuditCopy.set(true);
        }
        return;
      }
      if (
        this.autoSelectedDeploymentValidationAuditCopy() &&
        this.qcAuditCopyPath() === DEPLOYMENT_VALIDATION_AUDIT_COPY
      ) {
        this.qcAuditCopyPath.set('');
      }
      this.autoSelectedDeploymentValidationAuditCopy.set(false);
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
    if (this.startNow() && this.instanceAlreadyRunning()) {
      return `"${this.instanceId().trim()}" is already running. Stop it first, or turn off "Start trading immediately" to deploy without starting.`;
    }
    if (!this.required()) return 'Missing: ' + this.missingRequiredFields().join(', ') + '.';
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
      live_config: { sizing: this.resolveSizingPolicy() },
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
        max_orders_per_day: Number.isFinite(maxOrders) ? maxOrders : 50_000,
        ibkr_host: '127.0.0.1',
      };
    }
    try {
      const response = await this.svc.deployInstance(request);
      this.deployed.set(response);
      // A start-immediately deploy just made this instance live; refresh so the
      // guard blocks an immediate second start rather than waiting on a 409.
      this.instances.reload();
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
    this.autoSelectedDeploymentValidationAuditCopy.set(false);
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

  /** ADR 0009 — preset selector. Reference parity is gated by the audit-copy
   * allow-list (PR3); Custom ships in PR4. The radio rejects a stray switch to
   * a disabled option and refuses Reference parity when the gate isn't open. */
  setSizingPreset(e: Event): void {
    if (!(e.target instanceof HTMLInputElement)) return;
    const next = e.target.value;
    if (next === 'reference_parity' && !this.referenceParityAvailable()) {
      return;
    }
    if (next === 'safe_canary' || next === 'reference_parity' || next === 'custom') {
      this.sizingPreset.set(next);
    }
  }

  /** Map the selected preset into the canonical `SizingPolicy`. Reference
   * parity routes through `SetHoldings(1.0)` (gated server-side). Custom
   * resolves the operator's kind + value, validating that the value parses
   * as a positive number for FixedShares (integer ≥ 1) and as a positive
   * decimal string for FixedNotional. */
  private resolveSizingPolicy(): SizingPolicy {
    const preset = this.sizingPreset();
    if (preset === 'reference_parity') {
      return REFERENCE_PARITY_POLICY;
    }
    if (preset === 'custom') {
      const raw = this.customValue().trim();
      if (this.customKind() === 'FixedShares') {
        const n = Number.parseInt(raw, 10);
        if (!Number.isFinite(n) || n < 1) {
          throw new Error(`FixedShares value must be an integer ≥ 1, got "${raw}"`);
        }
        return { kind: 'FixedShares', value: n };
      }
      // FixedNotional ships the decimal as a string verbatim — Python's
      // Pydantic discriminated union rejects raw floats. We only sanity-check
      // here that the value parses as a positive number.
      const n = Number.parseFloat(raw);
      if (!Number.isFinite(n) || n <= 0) {
        throw new Error(`FixedNotional value must be a positive number, got ${raw}`);
      }
      return { kind: 'FixedNotional', value: raw };
    }
    return { kind: 'FixedShares', value: 1 };
  }

  setCustomKind(e: Event): void {
    if (!(e.target instanceof HTMLSelectElement)) return;
    const v = e.target.value;
    if (v === 'FixedShares' || v === 'FixedNotional') {
      this.customKind.set(v);
      // Re-default the value to a sane shape for the kind (1 share / 100 dollars).
      this.customValue.set(v === 'FixedShares' ? '1' : '100');
    }
  }

  setCustomValue(e: Event): void {
    if (e.target instanceof HTMLInputElement) {
      this.customValue.set(e.target.value);
    }
  }
}
