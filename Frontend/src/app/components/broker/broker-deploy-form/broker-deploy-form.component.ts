import { HttpErrorResponse } from '@angular/common/http';
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
  type IdentityCoherenceConfirmation,
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
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';
import {
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
} from './deploy-readiness';

// Kept in lockstep with the backend guard `identity._INSTANCE_ID_RE`
// (and `live_instances._INSTANCE_ID_RE`): a deployment name the operate
// endpoints reject (e.g. one with a space) must be caught here too, so the
// operator sees the reason inline instead of a created-but-unusable instance.
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

type DeployTabKey = 'strategy' | 'signal' | 'sizing' | 'legs' | 'launch';
type ExecutionMode = 'read_only' | 'paper_orders' | 'live';

interface DeployTab {
  key: DeployTabKey;
  label: string;
  target: string;
  complete: boolean;
}

interface IdentitySymbolEvidence {
  label: string;
  value: string;
  source: string;
}

interface IdentityCoherenceConflict {
  summary: string;
  signature: string;
  facts: IdentitySymbolEvidence[];
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
    ReceiptLabelPipe,
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
  readonly inheritedSymbol = signal<string>('');
  readonly inheritedSymbolSource = signal<string>('');
  readonly executionMode = signal<ExecutionMode>('paper_orders');
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
  private readonly signalStreamManuallyEdited = signal<boolean>(false);

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
      (strategy) =>
        strategy.validation_state === 'validated' &&
        strategy.deployable &&
        strategy.behavioral_equivalence?.verdict === 'accepted_for_deploy',
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

  readonly readonlyFlag = computed<boolean>(() => this.executionMode() === 'read_only');
  readonly executionCapability = computed<ExecutionMode>(() => this.executionMode());
  readonly executionCapabilityProof = computed<string>(() => {
    const mode = this.executionMode();
    if (mode === 'live') {
      return 'readonly_at_start: false · submission_capability: LIVE_ORDERS_BLOCKED';
    }
    return mode === 'read_only'
      ? 'readonly_at_start: true · submission_capability: READ_ONLY_OBSERVATION'
      : 'readonly_at_start: false · submission_capability: PAPER_ORDERS_ENABLED';
  });

  readonly actionPlanTradeSymbol = computed<string | null>(() =>
    BrokerDeployFormComponent.singleLongStockActionSymbol(this.actionPlan()),
  );
  private readonly identityCoherenceConfirmedSignature = signal<string | null>(null);
  readonly identityCoherenceEvidence = computed<IdentityCoherenceConflict | null>(() => {
    const inherited = normalizedSymbol(this.inheritedSymbol());
    if (!inherited) return null;
    const facts: IdentitySymbolEvidence[] = [
      {
        label: 'Inherited bot symbol',
        value: inherited,
        source: this.inheritedSymbolSource().trim() || 'request inherited symbol',
      },
    ];
    const signalStream = this.resolvedSignalStream();
    if (signalStream) {
      facts.push({
        label: 'Signal stream',
        value: signalStream,
        source: 'live_config.symbol',
      });
    }
    const actionSymbol = this.actionPlanTradeSymbol();
    if (actionSymbol) {
      facts.push({
        label: 'Action plan',
        value: actionSymbol,
        source: 'declared entry leg',
      });
    }
    if (facts.length < 2) return null;
    if (new Set(facts.map((fact) => fact.value)).size === 1) return null;

    const compared = facts
      .slice(1)
      .map((fact) => `${fact.label} ${fact.value}`)
      .join(' and ');
    return {
      summary: `Inherited bot symbol ${inherited} conflicts with ${compared}. Confirm the new run identity before Deploy & start.`,
      signature: facts.map((fact) => `${fact.label}:${fact.value}`).join('|'),
      facts,
    };
  });
  readonly identityCoherenceConfirmed = computed<boolean>(() => {
    const evidence = this.identityCoherenceEvidence();
    return evidence !== null && this.identityCoherenceConfirmedSignature() === evidence.signature;
  });
  readonly identityCoherenceConfirmation = computed<IdentityCoherenceConfirmation | null>(() => {
    if (!this.identityCoherenceConfirmed()) return null;
    const inherited = normalizedSymbol(this.inheritedSymbol());
    if (!inherited) return null;
    return {
      inherited_symbol: inherited,
      signal_stream: this.resolvedSignalStream() || null,
      action_plan_symbol: this.actionPlanTradeSymbol() ?? null,
    };
  });
  readonly identityCoherenceBlock = computed<IdentityCoherenceConflict | null>(() => {
    const evidence = this.identityCoherenceEvidence();
    if (evidence === null || !this.startNow() || this.identityCoherenceConfirmed()) {
      return null;
    }
    return evidence;
  });

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

  readonly deployReadinessFacts = computed(() =>
    buildDeployReadinessFacts({
      daemonState: this.connectivity.daemonState(),
      daemonFreshness: this.connectivity.daemonFreshness(),
      brokerState: this.connectivity.brokerState(),
      brokerDetail: this.connectivity.brokerDetail(),
      accountTruth: this.accountTruth.value(),
      brokerAccountAvailable: this.brokerAccountAvailable(),
      fleetState: this.connectivity.fleetState(),
      nothingDeployed: this.connectivity.nothingDeployed(),
    }),
  );

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

  readonly nowChecks = computed(() =>
    buildNowChecks({
      daemonState: this.connectivity.daemonState(),
      brokerState: this.connectivity.brokerState(),
      fieldsReady: this.fieldsReady(),
      fleetState: this.connectivity.fleetState(),
      nothingDeployed: this.connectivity.nothingDeployed(),
    }),
  );

  readonly deployChecks = computed(() => buildDeployChecks(this.error()?.status));

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
    const seedInheritedSymbol = qp.get('inherited_symbol');
    if (seedInheritedSymbol) this.inheritedSymbol.set(normalizedSymbol(seedInheritedSymbol));
    const seedInheritedSymbolSource = qp.get('inherited_symbol_source');
    if (seedInheritedSymbolSource) this.inheritedSymbolSource.set(seedInheritedSymbolSource.trim());
    const seedParent = qp.get('parent_run_id');
    if (seedParent) this.parentRunId.set(seedParent);
    const seedSignalStream = qp.get('signal_stream');
    if (seedSignalStream) {
      this.signalStreamManuallyEdited.set(true);
      this.signalStream.set(normalizedSymbol(seedSignalStream));
    }

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
      const validationSignal = normalizedSymbol(validation.validation_case_symbol);
      if (validationSignal && !this.signalStreamManuallyEdited()) {
        this.signalStream.set(validationSignal);
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
    if (this.startNow() && this.executionMode() === 'live') {
      return 'Live execution is not available from Deploy yet. Pick read-only or paper orders.';
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
    const identityConflict = this.identityCoherenceBlock();
    if (identityConflict !== null) return identityConflict.summary;
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
    const inheritedSymbol = normalizedSymbol(this.inheritedSymbol());
    if (inheritedSymbol) {
      request.inherited_symbol = inheritedSymbol;
      const source = this.inheritedSymbolSource().trim();
      if (source) request.inherited_symbol_source = source;
    }
    const identityConfirmation = this.identityCoherenceConfirmation();
    if (identityConfirmation !== null) {
      request.identity_coherence_confirmation = identityConfirmation;
    }
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
      this.seedIdentityCoherenceEvidence(err);
      this.error.set(toOperationError('deploy', err));
    } finally {
      this.busy.set(false);
    }
  }

  private seedIdentityCoherenceEvidence(err: unknown): void {
    if (!(err instanceof HttpErrorResponse)) return;
    const detail = (err.error as { detail?: unknown } | null | undefined)?.detail;
    if (!detail || typeof detail !== 'object') return;
    const payload = detail as Record<string, unknown>;
    if (payload['reason_code'] !== 'IDENTITY_COHERENCE_UNCONFIRMED') return;
    const evidence = payload['evidence'];
    if (!Array.isArray(evidence)) return;
    const inherited = evidence.find(
      (fact): fact is Record<string, unknown> =>
        Boolean(fact) &&
        typeof fact === 'object' &&
        (fact as Record<string, unknown>)['label'] === 'inherited_symbol',
    );
    const inheritedSymbol = normalizedSymbol(
      typeof inherited?.['value'] === 'string' ? inherited['value'] : '',
    );
    if (!inheritedSymbol) return;
    this.inheritedSymbol.set(inheritedSymbol);
    this.inheritedSymbolSource.set(
      typeof inherited?.['source'] === 'string' ? inherited['source'] : '',
    );
    this.identityCoherenceConfirmedSignature.set(null);
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
      this.signalStreamManuallyEdited.set(false);
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
    this.signalStreamManuallyEdited.set(false);
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
    this.signalStreamManuallyEdited.set(true);
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
  setExecutionMode(e: Event): void {
    if (e.target instanceof HTMLInputElement) {
      this.setExecutionModeValue(e.target.value);
    }
  }
  private setExecutionModeValue(value: string): void {
    if (value !== 'read_only' && value !== 'paper_orders' && value !== 'live') return;
    this.executionMode.set(value);
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

  confirmIdentityCoherence(): void {
    const evidence = this.identityCoherenceEvidence();
    if (evidence !== null) {
      this.identityCoherenceConfirmedSignature.set(evidence.signature);
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

}
