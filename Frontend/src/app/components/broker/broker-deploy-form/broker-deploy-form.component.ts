import {
  afterEveryRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { InputTextModule } from 'primeng/inputtext';
import {
  DEFAULT_MAX_ORDERS_PER_DAY,
  type HostRunnerDeployRequest,
  type HostRunnerDeployResponse,
  type HydratePolicy,
  type SizingPolicy,
  type SizingPreset,
  type SpecStrategyFixture,
} from '../../../api/live-runs.types';
import type { ActionPlan } from '../../../api/action-plan.types';
import { ActionPlanPickerComponent } from './action-plan-picker/action-plan-picker.component';
import { BrokerService } from '../../../services/broker.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { StrategyValidationService } from '../../../services/strategy-validation.service';
import type { StrategyValidationSummary } from '../../../services/strategy-validation.types';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';

// Kept in lockstep with the backend guard `identity._INSTANCE_ID_RE`
// (and `live_instances._INSTANCE_ID_RE`): a deployment name the operate
// endpoints reject (e.g. one with a space) must be caught here too, so the
// operator sees the reason inline instead of a created-but-unusable instance.
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

type DeployTabKey = 'strategy' | 'signal' | 'sizing' | 'legs' | 'launch';
type DeployReadinessState = 'ok' | 'warn' | 'down' | 'unknown';

interface DeployTab {
  key: DeployTabKey;
  label: string;
  target: string;
  complete: boolean;
}

interface DeployReadinessFact {
  key: 'engine' | 'broker' | 'account' | 'fleet';
  label: string;
  condition: string;
  detail: string;
  state: DeployReadinessState;
  link: string;
}

// ADR 0009 § 3 — Reference parity preset's policy. Pinned here as a constant
// so the gate lookup and the submit path use the *same* shape; a future change
// to the preset's all-in fraction needs to land in exactly one place.
const REFERENCE_PARITY_POLICY: SizingPolicy = { kind: 'SetHoldings', fraction: '1.0' };

function normalizedSymbol(value: string | null | undefined): string {
  return value?.trim().toUpperCase() ?? '';
}

/**
 * Deploy form for a live strategy instance. The UI uses plain operator words,
 * while the request still maps exactly to ADR 0006: create the run on the host,
 * bind it to a QC backtest receipt, and optionally start it immediately.
 */
@Component({
  selector: 'app-broker-deploy-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    BrokerConnectivityStripComponent,
    BrokerOperationResultComponent,
    ActionPlanPickerComponent,
    InputTextModule,
  ],
  templateUrl: './broker-deploy-form.component.html',
  styleUrl: './broker-deploy-form.component.scss',
})
export class BrokerDeployFormComponent {
  private readonly svc = inject(LiveRunsService);
  private readonly broker = inject(BrokerService);
  private readonly strategyValidation = inject(StrategyValidationService);
  protected readonly connectivity = inject(BrokerConnectivityService);
  private readonly route = inject(ActivatedRoute);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly destroyRef = inject(DestroyRef);

  readonly strategies = resource({ loader: () => this.svc.getEngineStrategies() });
  readonly strategyValidations = resource({ loader: () => this.strategyValidation.getCatalog() });
  readonly specFixtures = resource({ loader: () => this.svc.getSpecStrategyFixtures() });
  // Used only to pre-empt the daemon's "already active" 409: a start-immediately
  // deploy onto an instance that already has a live runner is rejected.
  readonly instances = resource({ loader: () => this.svc.getInstances() });
  // Display-only: the deploy boundary derives this from the connected broker
  // session and rejects deployment while broker identity is unavailable.
  readonly account = resource({ loader: () => this.broker.account() });
  readonly accountTruth = resource({ loader: () => this.broker.accountTruth() });
  // ADR 0009 § 9 — broker positions for the symbol-scoped all-in coexistence
  // guard (Decision 13). Loaded once on form open; the guard only consults it
  // when Reference parity is selected, so a broker outage doesn't block other
  // presets.
  readonly positions = resource({ loader: () => this.broker.positions() });

  // Form fields.
  readonly strategyKey = signal<string>('');
  readonly specPath = signal<string>('');
  readonly signalStream = signal<string>('');
  readonly manualSpecPath = signal<boolean>(false);
  readonly accountId = signal<string>('');
  readonly qcBacktestId = signal<string>('');
  readonly qcAuditCopyPath = signal<string>('');
  readonly instanceId = signal<string>('');
  readonly readonlyFlag = signal<boolean>(false);
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = signal<number>(DEFAULT_MAX_ORDERS_PER_DAY);
  readonly startNow = signal<boolean>(true);
  // PRD #593 Slice 1B (#595) — operator-declared action plan. Empty by
  // default; the picker mutates it in place. The submitted ``live_config``
  // always carries a plan (empty or otherwise) so ``run_id`` honestly
  // attests to declared intent; ADR 0012 §"Scope" says the engine
  // doesn't consume it until Slice 4.
  readonly actionPlan = signal<ActionPlan>({ on_enter: [], on_exit: [] });
  // PRD #593 Slice 1E (#598) — unhashed redeploy lineage. Seeded from
  // the cockpit's "Redeploy with changes" deep-link query param;
  // forwarded at the top level of the submit payload (NOT inside
  // ``live_config`` — lineage is unhashed; ADR 0012 §7).
  readonly parentRunId = signal<string | null>(null);
  // ADR 0009 § 7 — position-sizing preset. Defaults to Safe canary
  // (FixedShares(1)); the $250k surprise from the first deployment-validation
  // run is opt-in. Reference parity is gated by the audit-copy allow-list
  // (ADR § 3); Custom ships in PR4.
  readonly sizingPreset = signal<SizingPreset>('safe_canary');
  readonly activeDeployTab = signal<DeployTabKey>('strategy');

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
  readonly busy = signal<boolean>(false);
  readonly error = signal<OperationError | null>(null);
  readonly deployed = signal<HostRunnerDeployResponse | null>(null);
  readonly deployedInstanceId = signal<string | null>(null);
  readonly deployedBotControlLink = computed(() => {
    const id = this.deployedInstanceId();
    return id ? ['/broker/bots', id] : ['/broker/bots'];
  });

  // Captured once when the form opens, NOT per-submit: start_date_ms is part of
  // the content-addressed run_id hash, so a retry with identical inputs must
  // reuse the same value to hit the backend's idempotent no-op (created=false)
  // rather than minting a new run_id off the current clock.
  private readonly startDateMs = Date.now();

  readonly validatedStrategies = computed<StrategyValidationSummary[]>(() =>
    (this.strategyValidations.value()?.strategies ?? []).filter(
      (strategy) => strategy.validation_state === 'validated' && strategy.deployable,
    ),
  );

  readonly selectedValidation = computed<StrategyValidationSummary | null>(() => {
    const key = this.strategyKey().trim();
    if (!key) return null;
    return this.validatedStrategies().find((strategy) => strategy.strategy_key === key) ?? null;
  });

  readonly selectedFixture = computed<SpecStrategyFixture | null>(
    () => this.specFixtures.value()?.find((f) => f.path === this.specPath()) ?? null,
  );

  readonly fixtureSymbols = computed<string[]>(() =>
    [
      ...new Set(
        (this.selectedFixture()?.symbols ?? [])
          .map((symbol) => normalizedSymbol(symbol))
          .filter((symbol) => symbol !== ''),
      ),
    ],
  );

  readonly resolvedSignalStream = computed<string>(() => {
    return normalizedSymbol(this.signalStream());
  });

  /** ADR 0009 § 6 — the strategy's sizing surface. `"explicit"` (e.g.
   * `spy_ema_crossover_options`) means the algorithm sizes itself via internal
   * accounting (contracts_per_trade / market_order) and the live policy must
   * be `StrategyExplicit`; the sizing controls are disabled + labelled. */
  readonly selectedSizingSurface = computed<'policy' | 'explicit' | null>(() => {
    const strategy = this.strategyKey().trim();
    if (!strategy) return null;
    return this.strategies.value()?.find((s) => s.name === strategy)?.sizing_surface ?? null;
  });

  readonly sizingSurfaceIsExplicit = computed<boolean>(
    () => this.selectedSizingSurface() === 'explicit',
  );

  readonly brokerAccountAvailable = computed<boolean>(
    () => this.account.hasValue() && this.account.value() !== null,
  );

  private readonly brokerAccountId = computed<string>(
    () => (this.account.hasValue() ? (this.account.value()?.account_id ?? '') : ''),
  );

  readonly executionCapability = computed<'read_only' | 'paper_orders'>(() =>
    this.readonlyFlag() ? 'read_only' : 'paper_orders',
  );
  readonly executionCapabilityProof = computed<string>(() =>
    this.readonlyFlag()
      ? 'readonly_at_start: true · submission_capability: READ_ONLY_OBSERVATION'
      : 'readonly_at_start: false · submission_capability: PAPER_ORDERS_ENABLED',
  );

  readonly actionPlanTradeSymbol = computed<string | null>(() =>
    BrokerDeployFormComponent.singleLongStockActionSymbol(this.actionPlan()),
  );

  readonly deployTabs = computed<DeployTab[]>(() => [
    {
      key: 'strategy',
      label: 'Strategy',
      target: 'strategy-section',
      complete:
        this.selectedValidation() !== null &&
        this.specPath().trim() !== '' &&
        this.qcBacktestId().trim() !== '' &&
        this.qcAuditCopyPath().trim() !== '',
    },
    {
      key: 'signal',
      label: 'Signal stream',
      target: 'signal-section',
      complete: this.resolvedSignalStream() !== '',
    },
    {
      key: 'sizing',
      label: 'Sizing',
      target: 'sizing-section',
      complete: this.customSizingError() === null,
    },
    {
      key: 'legs',
      label: 'Legs',
      target: 'action-plan-picker-heading',
      complete: true,
    },
    {
      key: 'launch',
      label: 'Launch',
      target: 'launch-section',
      complete: this.instanceId().trim() !== '' && !this.instanceIdInvalid(),
    },
  ]);

  readonly deployReadinessFacts = computed<DeployReadinessFact[]>(() => [
    this.engineReadinessFact(),
    this.brokerReadinessFact(),
    this.accountReadinessFact(),
    this.fleetReadinessFact(),
  ]);

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
    if (this.resolvedSignalStream() === '') missing.push('Signal stream');
    if (this.accountId().trim() === '') missing.push('Connected broker account');
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
    // Re-deploy prefill: query params seed operator/runtime fields from the
    // prior run. Validated-strategy provenance below still wins for settings,
    // QC backtest, and audit copy; deploy does not let stale URLs re-author
    // technical evidence.
    const qp = this.route.snapshot.queryParamMap;
    const seedStrategy = qp.get('strategy_key');
    if (seedStrategy) this.strategyKey.set(seedStrategy);
    const seedSpecPath = qp.get('spec_path');
    if (seedSpecPath) {
      // Preserved only until the selected strategy's validation receipt loads.
      this.manualSpecPath.set(true);
      this.specPath.set(seedSpecPath);
    }
    const seedBacktestId = qp.get('qc_backtest_id');
    if (seedBacktestId) this.qcBacktestId.set(seedBacktestId);
    const seedAuditCopy = qp.get('qc_audit_copy_path');
    if (seedAuditCopy) this.qcAuditCopyPath.set(seedAuditCopy);
    const seedInstanceId = qp.get('instance_id');
    if (seedInstanceId) this.instanceId.set(seedInstanceId);
    const seedParent = qp.get('parent_run_id');
    if (seedParent) this.parentRunId.set(seedParent);
    const seedSignalStream = qp.get('signal_stream');
    if (seedSignalStream) this.signalStream.set(normalizedSymbol(seedSignalStream));

    effect(() => {
      const validation = this.selectedValidation();
      if (!validation) return;
      this.manualSpecPath.set(false);
      if (validation.settings_file_ref && this.specPath() !== validation.settings_file_ref) {
        this.specPath.set(validation.settings_file_ref);
      }
      if (validation.qc_cloud_backtest_id && this.qcBacktestId() !== validation.qc_cloud_backtest_id) {
        this.qcBacktestId.set(validation.qc_cloud_backtest_id);
      }
      if (validation.audit_copy_ref && this.qcAuditCopyPath() !== validation.audit_copy_ref) {
        this.qcAuditCopyPath.set(validation.audit_copy_ref);
      }
    });

    effect(() => {
      if (this.manualSpecPath()) return;
      if (this.selectedValidation() !== null) return;
      const strategy = this.strategyKey();
      const fixtures = this.specFixtures.value() ?? [];
      const match = fixtures.find((f) => f.name === strategy);
      const nextPath = match?.path;
      if (nextPath && this.specPath() !== nextPath) {
        this.specPath.set(nextPath);
      }
    });
    effect(() => {
      this.accountId.set(this.brokerAccountId());
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

    afterEveryRender(() => {
      this.syncRenderedFieldValues({ includeEmpty: false, onlyEmptySignals: true });
    });
    const restoreSyncHandle = window.setInterval(() => {
      this.syncRenderedFieldValues({ includeEmpty: false, onlyEmptySignals: true });
    }, 250);
    this.destroyRef.onDestroy(() => window.clearInterval(restoreSyncHandle));
  }

  private readonly required = computed<boolean>(
    () =>
      this.strategyKey().trim() !== '' &&
      this.specPath().trim() !== '' &&
      this.resolvedSignalStream() !== '' &&
      this.accountId().trim() !== '' &&
      this.qcBacktestId().trim() !== '' &&
      this.qcAuditCopyPath().trim() !== '' &&
      this.instanceId().trim() !== '',
  );

  /** ADR 0009 § 9 / Decision 13 — symbol-scoped all-in coexistence guard
   * surfaced client-side from the broker positions snapshot. Refuses
   * Reference parity (the only all-in preset) when the strategy's symbol
   * carries any exposure on the connected broker account. Cross-symbol
   * all-in concurrency is permitted-but-unsafe (the capital-sleeve layer
   * closes it later), so this guard intentionally only blocks the trade
   * symbol's own exposure.
   */
  readonly allInCoexistenceBlock = computed<string | null>(() => {
    if (this.sizingPreset() !== 'reference_parity') return null;
    const symbol = this.actionPlanTradeSymbol() ?? this.resolvedSignalStream();
    if (!symbol) return null;
    const snap = this.positions.value();
    if (!snap) return null;
    const own = snap.positions.find((p) => p.symbol.toUpperCase() === symbol);
    if (!own || Number(own.quantity) === 0) return null;
    return (
      `Reference parity blocked: ${symbol} already holds ${own.quantity} share(s) on this account. ` +
      'Flatten the position, or pick Safe canary / Custom — the capital-sleeve layer that would let ' +
      'two all-in bots coexist on one symbol is not built yet.'
    );
  });

  /** Why Deploy can't be submitted, sourced from the connectivity strip + form.
   * Null = ready. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.connectivity.daemonDown()) {
      return 'Live engine unavailable. Start it on this machine, then recheck.';
    }
    const coexistence = this.allInCoexistenceBlock();
    if (coexistence !== null) return coexistence;
    if (this.startNow() && this.connectivity.fleetBlocksStarts()) {
      return 'Fleet state blocks new starts. Turn off "Start trading immediately" to deploy only, or clear the account state.';
    }
    if (this.startNow() && this.instanceAlreadyRunning()) {
      return `"${this.instanceId().trim()}" is already running. Stop it first, or turn off "Start trading immediately" to deploy without starting.`;
    }
    if (!this.brokerAccountAvailable()) {
      return 'Connected broker account unavailable. Connect the broker session before deploying.';
    }
    const accountProof = this.accountTruth.value();
    if (this.startNow()) {
      if (!accountProof) {
        return 'Account proof is still loading. Wait for account readiness, or turn off "Start trading immediately" to deploy only.';
      }
      if (accountProof.final_verdict === 'not_proven') {
        return 'Account NOT_PROVEN. Reconcile account before starting, or turn off "Start trading immediately" to deploy only.';
      }
    }
    if (this.strategyKey().trim() !== '' && this.selectedValidation() === null) {
      return 'Strategy must be validated before deployment. Open Strategy Validation to promote it.';
    }
    if (!this.required()) return 'Missing: ' + this.missingRequiredFields().join(', ') + '.';
    // PR4 reviewer fix: surface invalid Custom sizing here so the deploy
    // button disables BEFORE submit() runs; throwing inside submit() would
    // leave busy=true and the form wedged.
    const customError = this.customSizingError();
    if (customError !== null) return customError;
    return null;
  });

  readonly canSubmit = computed<boolean>(() => !this.busy() && this.blockedReason() === null);

  async submit(): Promise<void> {
    this.syncRenderedFieldValues();
    if (!this.canSubmit()) return;
    this.busy.set(true);
    this.error.set(null);
    this.deployed.set(null);
    this.deployedInstanceId.set(null);
    const strategyKey = this.strategyKey().trim();
    const request: HostRunnerDeployRequest = {
      strategy_spec_path: this.specPath().trim(),
      qc_audit_copy_path: this.qcAuditCopyPath().trim(),
      qc_cloud_backtest_id: this.qcBacktestId().trim(),
      start_date_ms: this.startDateMs,
      strategy_instance_id: this.instanceId().trim(),
      strategy_key: strategyKey,
      live_config: {
        symbol: this.resolvedSignalStream(),
        sizing: this.resolveSizingPolicy(),
        action: this.actionPlan(),
      },
      start: this.startNow(),
    };
    const parent = this.parentRunId();
    if (parent) request.parent_run_id = parent;
    // Only attach launch knobs when actually starting — otherwise a deploy-only
    // request carries irrelevant start_options that still get validated (and a
    // cleared "max orders" field would serialize NaN → null and fail).
    if (this.startNow()) {
      const maxOrders = this.maxOrdersPerDay();
      request.start_options = {
        readonly: this.readonlyFlag(),
        hydrate_policy: this.hydratePolicy(),
        strategy: strategyKey,
        max_orders_per_day: Number.isFinite(maxOrders) ? maxOrders : DEFAULT_MAX_ORDERS_PER_DAY,
        ibkr_host: '127.0.0.1',
      };
    }
    try {
      const response = await this.svc.deployInstance(request);
      this.deployed.set(response);
      this.deployedInstanceId.set(request.strategy_instance_id);
      // A start-immediately deploy just made this instance live; refresh so the
      // guard blocks an immediate second start rather than waiting on a 409.
      this.instances.reload();
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
  private renderedFieldValue(
    field:
      | 'strategyKey'
      | 'specPath'
      | 'signalStream'
      | 'accountId'
      | 'qcBacktestId'
      | 'qcAuditCopyPath'
      | 'instanceId',
  ): string | null {
    const control = this.host.nativeElement.querySelector(
      `[data-deploy-field="${field}"]`,
    );
    if (control instanceof HTMLInputElement || control instanceof HTMLSelectElement) {
      return control.value;
    }
    return null;
  }

  private fixtureSymbolsForPath(path: string): string[] {
    const fixture = (this.specFixtures.value() ?? []).find((f) => f.path === path);
    return [
      ...new Set(
        (fixture?.symbols ?? [])
          .map((symbol) => normalizedSymbol(symbol))
          .filter((symbol) => symbol !== ''),
      ),
    ];
  }

  private seedSignalStreamFromFixturePath(path: string): void {
    const symbols = this.fixtureSymbolsForPath(path);
    if (symbols.length === 1) {
      this.signalStream.set(symbols[0]);
      return;
    }
    const current = normalizedSymbol(this.signalStream());
    if (symbols.length > 1 && !symbols.includes(current)) {
      this.signalStream.set('');
    }
  }

  private shouldSyncRenderedValue(
    renderedValue: string | null,
    signalValue: string,
    includeEmpty: boolean,
    onlyEmptySignals: boolean,
  ): renderedValue is string {
    return (
      renderedValue !== null &&
      (includeEmpty || renderedValue.trim() !== '') &&
      (!onlyEmptySignals || signalValue.trim() === '') &&
      renderedValue !== signalValue
    );
  }

  syncRenderedFieldValues(options: { includeEmpty?: boolean; onlyEmptySignals?: boolean } = {}): void {
    const includeEmpty = options.includeEmpty ?? true;
    const onlyEmptySignals = options.onlyEmptySignals ?? false;
    const strategyKey = this.renderedFieldValue('strategyKey');
    if (this.shouldSyncRenderedValue(strategyKey, this.strategyKey(), includeEmpty, onlyEmptySignals)) {
      this.manualSpecPath.set(false);
      this.strategyKey.set(strategyKey);
    }

    const specPath = this.renderedFieldValue('specPath');
    if (this.shouldSyncRenderedValue(specPath, this.specPath(), includeEmpty, onlyEmptySignals)) {
      this.specPath.set(specPath);
      if (!this.manualSpecPath()) this.seedSignalStreamFromFixturePath(specPath);
    }

    const signalStream = this.renderedFieldValue('signalStream');
    if (this.shouldSyncRenderedValue(signalStream, this.signalStream(), includeEmpty, onlyEmptySignals)) {
      this.signalStream.set(normalizedSymbol(signalStream));
    }

    const qcBacktestId = this.renderedFieldValue('qcBacktestId');
    if (this.shouldSyncRenderedValue(qcBacktestId, this.qcBacktestId(), includeEmpty, onlyEmptySignals)) {
      this.qcBacktestId.set(qcBacktestId);
    }

    const qcAuditCopyPath = this.renderedFieldValue('qcAuditCopyPath');
    if (this.shouldSyncRenderedValue(qcAuditCopyPath, this.qcAuditCopyPath(), includeEmpty, onlyEmptySignals)) {
      this.qcAuditCopyPath.set(qcAuditCopyPath);
    }

    const instanceId = this.renderedFieldValue('instanceId');
    if (this.shouldSyncRenderedValue(instanceId, this.instanceId(), includeEmpty, onlyEmptySignals)) {
      this.instanceId.set(instanceId);
    }
  }

  setStrategyKey(e: Event): void {
    this.manualSpecPath.set(false);
    this.strategyKey.set(this.text(e));
  }
  setSpecPath(e: Event): void {
    this.specPath.set(this.text(e));
  }
  setSpecFixturePath(e: Event): void {
    this.manualSpecPath.set(false);
    const path = this.text(e);
    this.specPath.set(path);
    this.seedSignalStreamFromFixturePath(path);
  }
  setSignalStream(e: Event): void {
    this.signalStream.set(normalizedSymbol(this.text(e)));
  }
  useManualSpecPath(): void {
    this.manualSpecPath.set(true);
  }
  setAccountId(e: Event): void {
    void e;
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
      this.readonlyFlag.set(e.target.value !== 'paper_orders');
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
    }
  }

  setActiveDeployTab(key: DeployTabKey): void {
    this.activeDeployTab.set(key);
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

  /** PR4 reviewer fix — strict integer regex so values like "1.9" or "25abc"
   * (which `Number.parseInt` happily truncates to 1 or 25) are rejected at
   * the form boundary, not silently truncated into a live order. */
  private static readonly FIXED_SHARES_INTEGER_RE = /^[1-9]\d*$/;
  /** PR4 reviewer fix — strict positive-decimal regex for FixedNotional. The
   * value travels to Python as a decimal string (no float on the wire), so we
   * only need to enforce a positive decimal shape here. */
  private static readonly FIXED_NOTIONAL_DECIMAL_RE = /^(?:\d+\.\d+|\d+\.?|\.\d+)$/;

  /** Validate the Custom preset's raw value against its kind. Returns a
   * user-facing error string when invalid (rendered via `blockedReason` so the
   * deploy button disables); returns `null` when the value is acceptable.
   * Centralizing this here means `submit()` never throws mid-flight after
   * setting `busy=true` (the PR4 reviewer's wedged-state concern). */
  readonly customSizingError = computed<string | null>(() => {
    if (this.sizingPreset() !== 'custom') return null;
    const raw = this.customValue().trim();
    if (raw === '') return 'Custom sizing value is required.';
    if (this.customKind() === 'FixedShares') {
      if (!BrokerDeployFormComponent.FIXED_SHARES_INTEGER_RE.test(raw)) {
        return `FixedShares value must be a whole number ≥ 1 (no decimals, letters, or signs). Got "${raw}".`;
      }
      const n = Number.parseInt(raw, 10);
      if (n < 1) return `FixedShares value must be ≥ 1. Got "${raw}".`;
      return null;
    }
    // FixedNotional
    if (!BrokerDeployFormComponent.FIXED_NOTIONAL_DECIMAL_RE.test(raw)) {
      return `FixedNotional value must be a positive number. Got "${raw}".`;
    }
    const n = Number.parseFloat(raw);
    if (!Number.isFinite(n) || n <= 0) {
      return `FixedNotional value must be a positive number. Got "${raw}".`;
    }
    return null;
  });

  /** Map the selected preset into the canonical `SizingPolicy`. Custom inputs
   * are validated upstream by `customSizingError`, which gates `canSubmit` —
   * so this method only runs when validation already passed and never
   * throws. */
  private resolveSizingPolicy(): SizingPolicy {
    // ADR 0009 § 6 — explicit-surface strategies submit the honest
    // `StrategyExplicit` policy, never a misleading FixedShares(1).
    if (this.sizingSurfaceIsExplicit()) {
      return { kind: 'StrategyExplicit' };
    }
    const preset = this.sizingPreset();
    if (preset === 'reference_parity') {
      return REFERENCE_PARITY_POLICY;
    }
    if (preset === 'custom') {
      const raw = this.customValue().trim();
      if (this.customKind() === 'FixedShares') {
        return { kind: 'FixedShares', value: Number.parseInt(raw, 10) };
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

  private static singleLongStockActionSymbol(action: ActionPlan): string | null {
    if (action.on_enter.length !== 1) return null;
    const [leg] = action.on_enter;
    if (leg.position !== 'long' || leg.instrument.kind !== 'stock') return null;
    const symbol = normalizedSymbol(leg.instrument.underlying);
    return symbol || null;
  }

  private engineReadinessFact(): DeployReadinessFact {
    const state = this.connectivity.daemonState();
    const freshness = this.connectivity.daemonFreshness();
    if (state === 'down') {
      return {
        key: 'engine',
        label: 'Engine',
        condition: 'Unreachable',
        detail: 'Start the local daemon, then recheck.',
        state: 'down',
        link: '/engine',
      };
    }
    if (freshness.state === 'stale') {
      return {
        key: 'engine',
        label: 'Engine',
        condition: 'Stale code',
        detail: 'Restart the daemon to apply the current repo.',
        state: 'warn',
        link: '/engine',
      };
    }
    if (state === 'ok') {
      return {
        key: 'engine',
        label: 'Engine',
        condition: 'Healthy',
        detail: freshness.sha ? `Running ${freshness.sha}` : 'Daemon reachable.',
        state: 'ok',
        link: '/engine',
      };
    }
    return {
      key: 'engine',
      label: 'Engine',
      condition: 'Checking',
      detail: 'Waiting for daemon health.',
      state: 'unknown',
      link: '/engine',
    };
  }

  private brokerReadinessFact(): DeployReadinessFact {
    const state = this.connectivity.brokerState();
    const condition =
      state === 'ok'
        ? 'Linked'
        : state === 'down'
          ? 'Hard down'
          : state === 'warn'
            ? 'Reconnecting'
            : 'Checking';
    return {
      key: 'broker',
      label: 'Broker',
      condition,
      detail: this.connectivity.brokerDetail(),
      state,
      link: '/broker/session-mirror',
    };
  }

  private accountReadinessFact(): DeployReadinessFact {
    const truth = this.accountTruth.value();
    if (!this.brokerAccountAvailable()) {
      return {
        key: 'account',
        label: 'Account',
        condition: 'Not proven',
        detail: 'Broker account identity is unavailable.',
        state: 'down',
        link: '/broker/account-monitor',
      };
    }
    if (truth?.final_verdict === 'not_proven') {
      return {
        key: 'account',
        label: 'Account',
        condition: 'Not proven',
        detail: truth.status_detail,
        state: 'warn',
        link: '/broker/account-monitor',
      };
    }
    if (truth?.final_verdict === 'clean') {
      return {
        key: 'account',
        label: 'Account',
        condition: 'Clean',
        detail: truth.status_detail,
        state: 'ok',
        link: '/broker/account-monitor',
      };
    }
    return {
      key: 'account',
      label: 'Account',
      condition: 'Checking',
      detail: 'Waiting for account-truth proof.',
      state: 'unknown',
      link: '/broker/account-monitor',
    };
  }

  private fleetReadinessFact(): DeployReadinessFact {
    const state = this.connectivity.fleetState();
    if (this.connectivity.nothingDeployed()) {
      return {
        key: 'fleet',
        label: 'Fleet',
        condition: 'Empty',
        detail: 'No deployed bots on this account.',
        state: 'unknown',
        link: '/broker/bots',
      };
    }
    if (state === 'warn') {
      return {
        key: 'fleet',
        label: 'Fleet',
        condition: 'Contaminated',
        detail: 'New starts are blocked until account state clears.',
        state: 'warn',
        link: '/broker/reconciliation',
      };
    }
    if (state === 'ok') {
      return {
        key: 'fleet',
        label: 'Fleet',
        condition: 'Clear',
        detail: 'No fleet policy blocks.',
        state: 'ok',
        link: '/broker/bots',
      };
    }
    return {
      key: 'fleet',
      label: 'Fleet',
      condition: 'Checking',
      detail: 'Waiting for fleet policy.',
      state: 'unknown',
      link: '/broker/bots',
    };
  }
}
