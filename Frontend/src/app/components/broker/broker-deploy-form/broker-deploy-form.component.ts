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
import type { AccountTriageResponse } from '../../../api/account-reconciliation.types';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import {
  DEFAULT_MAX_ORDERS_PER_DAY,
  type ExposureCoherencePosture,
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
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';
import {
  deployPrefillParamsFromQuery,
  isExposurePosture,
  normalizeExposurePositionsRecord,
  normalizedSymbol,
  singleLongStockActionSymbol,
} from '../lib/deploy-prefill-params';
import {
  buildExposureCoherenceConfirmation,
  buildExposureCoherenceEvidence,
  buildIdentityCoherenceConfirmation,
  buildIdentityCoherenceEvidence,
  exposureCoherenceCardFacts,
  type ExposureCoherenceConflict,
  identityCoherenceCardFacts,
  type IdentityCoherenceConflict,
} from './deploy-coherence';
import { DeployCoherenceCardComponent } from './deploy-coherence-card.component';
import {
  type AccountProofBlock,
  actionPlanDeployReadiness,
  buildDeployChecks,
  buildDeployReadinessFacts,
  buildNowChecks,
  type DeployBlocker,
  deployBlocker,
  stoppedStartLatchState,
} from './deploy-readiness';

// Mirror the backend single-segment deployment name guard.
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

type DeployTabKey = 'strategy' | 'signal' | 'sizing' | 'legs' | 'launch';
type ExecutionMode = 'read_only' | 'paper_orders' | 'live';

interface DeployTab {
  key: DeployTabKey;
  label: string;
  target: string;
  complete: boolean;
}

interface DeployCommandState {
  kind: 'busy' | 'accepted' | 'blocked' | 'ready';
  message: string;
  canSubmit: boolean;
  actionLink?: AccountProofBlock;
}

// ADR 0009 § 3: gate lookup and submit share the same Reference parity policy.
const REFERENCE_PARITY_POLICY: SizingPolicy = { kind: 'SetHoldings', fraction: '1.0' };

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
    DeployCoherenceCardComponent,
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
  readonly instances = resource({ loader: () => this.svc.getInstances() });
  readonly instanceStatus = resource<LiveInstanceStatus | null, string | null>({
    params: () => {
      const id = this.instanceId().trim();
      return this.startNow() && id !== '' && INSTANCE_ID_RE.test(id) ? id : null;
    },
    loader: ({ params }) => this.loadInstanceStatus(params),
  });
  readonly account = resource({ loader: () => this.broker.account() });
  readonly accountTruth = resource({ loader: () => this.broker.accountTruth() });
  readonly accountTriage = resource<AccountTriageResponse | null, string | null>({
    params: () => {
      const accountId = this.brokerAccountId();
      return accountId === '' ? null : accountId;
    },
    loader: ({ params }) => (params === null ? Promise.resolve(null) : this.broker.accountTriage(params)),
    defaultValue: null,
  });
  // ADR 0009 § 9: symbol-scoped all-in coexistence guard.
  readonly positions = resource({ loader: () => this.broker.positions() });

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
  readonly inheritedExposurePosture = signal<ExposureCoherencePosture | ''>('');
  readonly inheritedExposurePendingOrderCount = signal<number | null>(null);
  readonly inheritedExposurePositions = signal<Record<string, number>>({});
  readonly inheritedExposureSource = signal<string>('');
  readonly executionMode = signal<ExecutionMode>('paper_orders');
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = signal<number>(DEFAULT_MAX_ORDERS_PER_DAY);
  readonly startNow = signal<boolean>(true);
  readonly actionPlan = signal<ActionPlan>({ on_enter: [], on_exit: [] });
  readonly parentRunId = signal<string | null>(null);
  readonly sizingPreset = signal<SizingPreset>('safe_canary');
  readonly activeDeployTab = signal<DeployTabKey>('strategy');
  private readonly signalStreamManuallyEdited = signal<boolean>(false);

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

  // PR4: Custom values stay decimal-string-friendly until submit.
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
  readonly postSubmitCommandStatus = computed<string | null>(() => {
    const deployed = this.deployed();
    if (!deployed?.start?.accepted) return null;
    if (!this.startNow()) return null;
    if (this.deployedInstanceId() !== this.instanceId().trim()) return null;
    return `Start accepted for run ${deployed.run_id}. View deployment to monitor the live run.`;
  });
  readonly commandState = computed<DeployCommandState>(() => {
    if (this.busy()) {
      return { kind: 'busy', message: 'Submitting deployment.', canSubmit: false };
    }
    const accepted = this.postSubmitCommandStatus();
    if (accepted !== null) {
      return { kind: 'accepted', message: accepted, canSubmit: false };
    }
    const blocked = this.preSubmitBlocker();
    if (blocked !== null) {
      return {
        kind: 'blocked',
        message: blocked.message,
        canSubmit: false,
        actionLink: blocked.actionLink,
      };
    }
    if (this.stoppedStartLatch()) {
      return {
        kind: 'ready',
        message:
          'This bot is off duty. This submit will deploy only; run roll call on the bot page before starting.',
        canSubmit: true,
      };
    }
    return { kind: 'ready', message: 'Ready to deploy.', canSubmit: true };
  });
  readonly commandStatus = computed<string>(() => this.commandState().message);

  // Captured once so identical retries can hit the backend idempotent no-op.
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
    singleLongStockActionSymbol(this.actionPlan()) || null,
  );
  readonly actionPlanReadiness = computed(() =>
    actionPlanDeployReadiness(this.strategyKey(), this.actionPlan()),
  );
  private readonly identityCoherenceConfirmedSignature = signal<string | null>(null);
  readonly identityCoherenceEvidence = computed<IdentityCoherenceConflict | null>(() =>
    buildIdentityCoherenceEvidence({
      inheritedSymbol: this.inheritedSymbol(),
      inheritedSymbolSource: this.inheritedSymbolSource(),
      signalStream: this.resolvedSignalStream(),
      actionPlanSymbol: this.actionPlanTradeSymbol(),
    }),
  );
  readonly identityCoherenceConfirmed = computed<boolean>(() => {
    const evidence = this.identityCoherenceEvidence();
    return evidence !== null && this.identityCoherenceConfirmedSignature() === evidence.signature;
  });
  readonly identityCoherenceConfirmation = computed(() =>
    buildIdentityCoherenceConfirmation({
      confirmed: this.identityCoherenceConfirmed(),
      inheritedSymbol: this.inheritedSymbol(),
      signalStream: this.resolvedSignalStream(),
      actionPlanSymbol: this.actionPlanTradeSymbol(),
    }),
  );
  readonly identityCoherenceFacts = computed(() =>
    identityCoherenceCardFacts(this.identityCoherenceEvidence()),
  );
  readonly identityCoherenceBlock = computed<IdentityCoherenceConflict | null>(() => {
    const evidence = this.identityCoherenceEvidence();
    if (evidence === null || !this.effectiveStartNow() || this.identityCoherenceConfirmed()) {
      return null;
    }
    return evidence;
  });

  private readonly exposureCoherenceConfirmedSignature = signal<string | null>(null);
  readonly exposureCoherenceEvidence = computed<ExposureCoherenceConflict | null>(() =>
    buildExposureCoherenceEvidence({
      posture: this.inheritedExposurePosture(),
      pendingOrderCount: this.inheritedExposurePendingOrderCount(),
      ownedPositions: this.inheritedExposurePositions(),
      source: this.inheritedExposureSource(),
      instanceId: this.instanceId().trim(),
      parentRunId: this.parentRunId(),
    }),
  );
  readonly exposureCoherenceConfirmed = computed<boolean>(() => {
    const evidence = this.exposureCoherenceEvidence();
    return evidence !== null && this.exposureCoherenceConfirmedSignature() === evidence.signature;
  });
  readonly exposureCoherenceConfirmation = computed(() =>
    buildExposureCoherenceConfirmation({
      evidence: this.exposureCoherenceEvidence(),
      confirmed: this.exposureCoherenceConfirmed(),
      instanceId: this.instanceId().trim(),
      parentRunId: this.parentRunId(),
    }),
  );
  readonly exposureCoherenceFacts = computed(() =>
    exposureCoherenceCardFacts(this.exposureCoherenceEvidence()),
  );
  readonly exposureCoherenceBlock = computed<ExposureCoherenceConflict | null>(() => {
    const evidence = this.exposureCoherenceEvidence();
    if (evidence === null || !this.effectiveStartNow() || this.exposureCoherenceConfirmed()) {
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
      complete: this.actionPlanReadiness().canDeploy,
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
      accountTriage: this.accountTriage.value(),
      brokerAccountAvailable: this.brokerAccountAvailable(),
      fleetState: this.connectivity.fleetState(),
      nothingDeployed: this.connectivity.nothingDeployed(),
    }),
  );

  readonly instanceAlreadyRunning = computed<boolean>(() => {
    const id = this.instanceId().trim();
    if (id === '') return false;
    const match = this.instances.value()?.find((i) => i.strategy_instance_id === id);
    return match?.process_state === 'running' || match?.process_state === 'stopping';
  });

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
      accountTriage: this.accountTriage.value(),
    }),
  );

  readonly deployChecks = computed(() => buildDeployChecks(this.error()?.status));
  readonly stoppedStartLatchStatus = computed(() => {
    const statusUnavailable = this.instanceStatus.error() !== undefined;
    const status = statusUnavailable ? null : this.instanceStatus.value();
    const id = this.instanceId().trim();
    return stoppedStartLatchState({
      startNow: this.startNow(),
      instanceId: id,
      instanceIdValid: id !== '' && INSTANCE_ID_RE.test(id),
      statusLoading: this.instanceStatus.isLoading(),
      statusUnavailable,
      desiredState: status?.desired_state,
      startCapability: status?.operator_surface.host_process.start_capability,
    });
  });
  readonly stoppedStartLatch = computed<boolean>(() => this.stoppedStartLatchStatus() === 'blocked');
  readonly effectiveStartNow = computed<boolean>(() => this.startNow() && !this.stoppedStartLatch());
  readonly commandTitle = computed<string>(() =>
    this.effectiveStartNow() ? 'Deploy & start' : 'Deploy only',
  );
  readonly commandButtonLabel = computed<string>(() =>
    this.effectiveStartNow() ? 'Deploy & start' : 'Deploy',
  );
  constructor() {
    // Re-deploy URLs seed operator/runtime fields; validation receipts still win.
    const prefill = deployPrefillParamsFromQuery(this.route.snapshot.queryParamMap);
    if (prefill.strategyKey) this.strategyKey.set(prefill.strategyKey);
    if (prefill.specPath) {
      // Preserved only until the selected strategy's validation receipt loads.
      this.manualSpecPath.set(true);
      this.specPath.set(prefill.specPath);
    }
    if (prefill.qcBacktestId) this.qcBacktestId.set(prefill.qcBacktestId);
    if (prefill.qcAuditCopyPath) this.qcAuditCopyPath.set(prefill.qcAuditCopyPath);
    if (prefill.instanceId) this.instanceId.set(prefill.instanceId);
    if (prefill.inheritedSymbol) this.inheritedSymbol.set(prefill.inheritedSymbol);
    if (prefill.inheritedSymbolSource) this.inheritedSymbolSource.set(prefill.inheritedSymbolSource);
    if (prefill.inheritedExposurePosture) this.inheritedExposurePosture.set(prefill.inheritedExposurePosture);
    if (prefill.inheritedExposurePendingOrderCount !== null) {
      this.inheritedExposurePendingOrderCount.set(prefill.inheritedExposurePendingOrderCount);
    }
    this.inheritedExposurePositions.set(prefill.inheritedExposurePositions);
    if (prefill.inheritedExposureSource) this.inheritedExposureSource.set(prefill.inheritedExposureSource);
    if (prefill.parentRunId) this.parentRunId.set(prefill.parentRunId);
    if (prefill.signalStream) {
      this.signalStreamManuallyEdited.set(true);
      this.signalStream.set(prefill.signalStream);
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
    // ADR 0009 § 3: Reference parity cannot silently downgrade.
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

  /** Symbol-scoped Reference parity exposure guard. */
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

  private readonly preSubmitBlocker = computed<DeployBlocker | null>(() =>
    deployBlocker({
      daemonDown: this.connectivity.daemonDown(),
      effectiveStartNow: this.effectiveStartNow(),
      executionMode: this.executionMode(),
      allInCoexistenceBlock: this.allInCoexistenceBlock(),
      fleetBlocksStarts: this.connectivity.fleetBlocksStarts(),
      instanceAlreadyRunning: this.instanceAlreadyRunning(),
      instanceId: this.instanceId().trim(),
      brokerAccountAvailable: this.brokerAccountAvailable(),
      accountTruth: this.accountTruth.value(),
      accountTriage: this.accountTriage.value(),
      strategyKey: this.strategyKey(),
      strategySelected: this.selectedValidation() !== null,
      required: this.required(),
      missingRequiredFields: this.missingRequiredFields(),
      identityConflictSummary: this.identityCoherenceBlock()?.summary ?? null,
      exposureConflictSummary: this.exposureCoherenceBlock()?.summary ?? null,
      actionPlanReadiness: this.actionPlanReadiness(),
      customSizingError: this.customSizingError(),
      stoppedStartLatchState: this.stoppedStartLatchStatus(),
    }),
  );

  readonly activeBlocker = computed<DeployBlocker | null>(() => {
    const state = this.commandState();
    if (state.kind !== 'blocked') return null;
    return { message: state.message, actionLink: state.actionLink };
  });

  readonly blockedReason = computed<string | null>(() => {
    return this.activeBlocker()?.message ?? null;
  });

  readonly canSubmit = computed<boolean>(() => this.commandState().canSubmit);

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
      start: this.effectiveStartNow(),
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
    const exposurePosture = this.inheritedExposurePosture();
    if (exposurePosture) {
      request.inherited_exposure_posture = exposurePosture;
      request.inherited_exposure_pending_order_count = this.inheritedExposurePendingOrderCount();
      request.inherited_exposure_positions = this.inheritedExposurePositions();
      const source = this.inheritedExposureSource().trim();
      if (source) request.inherited_exposure_source = source;
    }
    const exposureConfirmation = this.exposureCoherenceConfirmation();
    if (exposureConfirmation !== null) {
      request.exposure_coherence_confirmation = exposureConfirmation;
    }
    // Only attach launch knobs when actually starting — otherwise a deploy-only
    // request carries irrelevant start_options that still get validated (and a
    // cleared "max orders" field would serialize NaN → null and fail).
    if (this.effectiveStartNow()) {
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
      this.seedExposureCoherenceEvidence(err);
      this.error.set(toOperationError('deploy', err));
    } finally {
      this.busy.set(false);
    }
  }

  private deployErrorDetail(err: unknown): Record<string, unknown> | null {
    if (!(err instanceof HttpErrorResponse)) return null;
    const detail = (err.error as { detail?: unknown } | null | undefined)?.detail;
    if (!detail || typeof detail !== 'object') return null;
    return detail as Record<string, unknown>;
  }

  private seedIdentityCoherenceEvidence(err: unknown): void {
    const payload = this.deployErrorDetail(err);
    if (payload === null) return;
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

  private seedExposureCoherenceEvidence(err: unknown): void {
    const payload = this.deployErrorDetail(err);
    if (payload === null) return;
    if (payload['reason_code'] !== 'EXPOSURE_COHERENCE_UNCONFIRMED') return;
    const evidence = payload['evidence'];
    if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return;
    const facts = evidence as Record<string, unknown>;
    const posture = facts['posture'];
    if (typeof posture !== 'string' || !isExposurePosture(posture)) return;

    const pendingOrderCount = facts['pending_order_count'];
    const ownedPositions = normalizeExposurePositionsRecord(facts['owned_positions']) ?? {};
    const source = facts['source'];
    const runId = facts['run_id'];
    const normalizedPendingOrderCount =
      typeof pendingOrderCount === 'number' &&
      Number.isInteger(pendingOrderCount) &&
      pendingOrderCount >= 0
        ? pendingOrderCount
        : null;
    this.inheritedExposurePosture.set(posture);
    this.inheritedExposurePendingOrderCount.set(normalizedPendingOrderCount);
    this.inheritedExposurePositions.set(ownedPositions);
    this.inheritedExposureSource.set(typeof source === 'string' ? source : '');
    if (typeof runId === 'string' && runId.trim()) {
      this.parentRunId.set(runId.trim());
    }
    this.exposureCoherenceConfirmedSignature.set(null);
  }

  // Event readers that narrow without a type assertion.
  private text(e: Event): string {
    return e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement
      ? e.target.value
      : '';
  }

  private async loadInstanceStatus(instanceId: string | null): Promise<LiveInstanceStatus | null> {
    if (instanceId === null) return null;
    return this.svc.getInstanceStatus(instanceId);
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

  confirmExposureCoherence(): void {
    const evidence = this.exposureCoherenceEvidence();
    if (evidence !== null) {
      this.exposureCoherenceConfirmedSignature.set(evidence.signature);
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

}
