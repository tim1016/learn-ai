import {
  ChangeDetectionStrategy,
  Component,
  computed,
  ElementRef,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import {
  FormField,
  form,
  max,
  min,
  pattern,
  required,
} from '@angular/forms/signals';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { TagModule } from 'primeng/tag';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import {
  DEFAULT_MAX_ORDERS_PER_DAY,
  type ExposureCoherencePosture,
  type HostRunnerDeployRequest,
  type HostRunnerDeployResponse,
  type HydratePolicy,
  type SizingPolicy,
  type SizingPreset,
} from '../../../api/live-runs.types';
import type { ActionPlan } from '../../../api/action-plan.types';
import { ActionPlanPickerComponent } from './action-plan-picker/action-plan-picker.component';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { StrategyValidationService } from '../../../services/strategy-validation.service';
import type { StrategyValidationSummary } from '../../../services/strategy-validation.types';
import { BrokerConnectivityStripComponent } from '../broker-connectivity-strip/broker-connectivity-strip.component';
import { BrokerOperationResultComponent } from '../broker-operation-result/broker-operation-result.component';
import { type OperationError, toOperationError } from '../operation-error';
import {
  deployPrefillParamsFromQuery,
  normalizedSymbol,
  singleLongStockActionSymbol,
} from '../lib/deploy-prefill-params';
import {
  buildExposureCoherenceConfirmation,
  buildExposureCoherenceEvidence,
  buildIdentityCoherenceConfirmation,
  buildIdentityCoherenceEvidence,
  exposureCoherenceSeedFromDeployError,
  exposureCoherenceCardFacts,
  exposureLaunchDecision as buildExposureLaunchDecision,
  type ExposureCoherenceConflict,
  type ExposureLaunchDecision,
  identityCoherenceCardFacts,
  identityCoherenceSeedFromDeployError,
  type IdentityCoherenceConflict,
} from './deploy-coherence';
import { DeployCoherenceCardComponent } from './deploy-coherence-card.component';
import { actionPlanDeployReadiness } from './deploy-readiness';
import {
  buildFormBlockers,
  deployReady,
  resolveBlockerMove,
} from './deploy-blockers';
import {
  OperatorBlockerListComponent,
  type OperatorBlockerMoveEvent,
} from '../shared/operator-blocker-list/operator-blocker-list.component';
import type {
  DeployPreflightResponse,
  OperatorBlocker,
} from '../../../api/operator-blocker.types';
import {
  REFERENCE_PARITY_POLICY,
  type CustomSizingKind,
  customSizingValidationMessage,
  resolveDeploySizingPolicy,
} from './deploy-sizing';
import { ExposureLaunchDecisionComponent } from './exposure-launch-decision.component';

// Mirror the backend single-segment deployment name guard.
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

interface DeployTicketModel {
  instanceId: string;
  strategyKey: string;
  signalStream: string;
  maxOrdersPerDay: number;
  customKind: CustomSizingKind;
  customValue: string;
}

type ExecutionMode = 'read_only' | 'paper_orders' | 'live';
type CoherenceRecoveryKind = 'identity' | 'exposure';

interface DeployCommandState {
  kind: 'busy' | 'accepted' | 'blocked' | 'ready';
  message: string;
  canSubmit: boolean;
}

interface DeployPreflightParams {
  strategyKey: string;
  accountId: string;
  instanceId: string;
}

interface SettledDeployPreflight {
  params: DeployPreflightParams;
  response: DeployPreflightResponse;
}

@Component({
  selector: 'app-broker-deploy-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    BrokerConnectivityStripComponent,
    BrokerOperationResultComponent,
    ActionPlanPickerComponent,
    ButtonModule,
    InputTextModule,
    TagModule,
    ReceiptLabelPipe,
    FormField,
    DeployCoherenceCardComponent,
    ExposureLaunchDecisionComponent,
    OperatorBlockerListComponent,
  ],
  templateUrl: './broker-deploy-form.component.html',
  styleUrl: './broker-deploy-form.component.scss',
})
export class BrokerDeployFormComponent {
  private readonly svc = inject(LiveRunsService);
  private readonly broker = inject(BrokerService);
  private readonly strategyValidation = inject(StrategyValidationService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  readonly strategies = resource({ loader: () => this.svc.getEngineStrategies() });
  readonly strategyValidations = resource({ loader: () => this.strategyValidation.getCatalog() });
  readonly account = resource({ loader: () => this.broker.account() });
  // ADR 0009 § 9: symbol-scoped all-in coexistence guard.
  readonly positions = resource({ loader: () => this.broker.positions() });
  readonly deployPreflight = resource<
    DeployPreflightResponse | null,
    DeployPreflightParams | null
  >({
    params: () => this.deployPreflightRequest(),
    loader: ({ params }) =>
      params === null ? Promise.resolve(null) : this.svc.deployPreflight(params),
    defaultValue: null,
  });
  private readonly settledDeployPreflight = signal<SettledDeployPreflight | null>(null);

  /**
   * The signal-form model is the sole source of truth for trader-entered
   * ticket values. Derived signals below preserve the concise domain reads
   * used by the launch, preflight, and safety-gate code.
   */
  readonly ticket = signal<DeployTicketModel>({
    instanceId: '',
    strategyKey: '',
    signalStream: '',
    maxOrdersPerDay: DEFAULT_MAX_ORDERS_PER_DAY,
    customKind: 'FixedShares',
    customValue: '1',
  });
  readonly ticketForm = form(this.ticket, (ticket) => {
    required(ticket.instanceId, { message: 'Enter a deployment name.' });
    pattern(ticket.instanceId, INSTANCE_ID_RE, {
      message: 'Use letters, numbers, periods, underscores, or hyphens.',
    });
    required(ticket.strategyKey, { message: 'Choose an approved strategy.' });
    required(ticket.signalStream, { message: 'Enter the market-data stream this strategy should read.' });
    min(ticket.maxOrdersPerDay, 0);
    max(ticket.maxOrdersPerDay, 100_000);
  });

  readonly strategyKey = computed(() => this.ticket().strategyKey);
  readonly specPath = signal<string>('');
  readonly signalStream = computed(() => this.ticket().signalStream);
  readonly accountId = signal<string>('');
  readonly qcBacktestId = signal<string>('');
  readonly qcAuditCopyPath = signal<string>('');
  readonly instanceId = computed(() => this.ticket().instanceId);
  readonly inheritedSymbol = signal<string>('');
  readonly inheritedSymbolSource = signal<string>('');
  readonly inheritedExposurePosture = signal<ExposureCoherencePosture | ''>('');
  readonly inheritedExposurePendingOrderCount = signal<number | null>(null);
  readonly inheritedExposurePositions = signal<Record<string, number>>({});
  readonly inheritedExposureSource = signal<string>('');
  readonly executionMode = signal<ExecutionMode>('paper_orders');
  readonly hydratePolicy = signal<HydratePolicy>('require');
  readonly maxOrdersPerDay = computed(() => this.ticket().maxOrdersPerDay);
  readonly actionPlan = signal<ActionPlan>({ on_enter: [], on_exit: [] });
  readonly parentRunId = signal<string | null>(null);
  readonly sizingPreset = signal<SizingPreset>('safe_canary');
  private readonly signalStreamManuallyEdited = signal<boolean>(false);
  private readonly executionModeTouched = signal<boolean>(false);
  private readonly legsTouched = signal<boolean>(false);
  private readonly activeCoherenceRecovery = signal<CoherenceRecoveryKind | null>(null);

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

  // PR4: Custom values stay decimal-string-friendly until submit.
  readonly customKind = computed(() => this.ticket().customKind);
  readonly customValue = computed(() => this.ticket().customValue);
  readonly busy = signal<boolean>(false);
  readonly error = signal<OperationError | null>(null);
  readonly deployed = signal<HostRunnerDeployResponse | null>(null);
  readonly deployedInstanceId = signal<string | null>(null);
  readonly preparedForCohort = signal<boolean>(false);
  readonly deployedBotControlLink = computed(() => {
    const id = this.deployedInstanceId();
    return id ? ['/broker/bots', id] : ['/broker/bots'];
  });
  readonly postSubmitCommandStatus = computed<string | null>(() => {
    const deployed = this.deployed();
    if (!deployed?.start?.accepted) return null;
    if (this.deployedInstanceId() !== this.instanceId().trim()) return null;
    return `Start accepted for run ${deployed.run_id}. View deployment to monitor the live run.`;
  });
  readonly visibleDeployPreflight = computed<DeployPreflightResponse | null>(() => {
    const current = this.deployPreflight.value();
    if (current !== null) return current;
    const settled = this.settledDeployPreflight();
    const params = this.deployPreflightRequest();
    if (params === null || settled === null) return null;
    if (
      settled.params.strategyKey !== params.strategyKey ||
      settled.params.accountId !== params.accountId
    ) {
      return null;
    }
    return settled.response;
  });
  readonly commandState = computed<DeployCommandState>(() => {
    if (this.busy()) {
      return { kind: 'busy', message: 'Launching strategy…', canSubmit: false };
    }
    const accepted = this.postSubmitCommandStatus();
    if (accepted !== null) {
      return { kind: 'accepted', message: accepted, canSubmit: false };
    }
    if (this.deployPreflight.isLoading() && this.visibleDeployPreflight() === null) {
      return { kind: 'blocked', message: 'Checking server launch gates…', canSubmit: false };
    }
    const top = this.topBlocker();
    if (top !== null) {
      return { kind: 'blocked', message: `Launch blocked: ${top.headline}.`, canSubmit: false };
    }
    return { kind: 'ready', message: 'Ready to launch.', canSubmit: true };
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

  readonly validatedReceiptIssue = computed<string | null>(() => {
    const selected = this.selectedValidation();
    if (this.strategyKey().trim() === '') return null;
    if (selected === null) {
      return 'This strategy is not currently approved for deployment. Select a strategy from the approved list.';
    }
    if (
      selected.settings_file_ref?.trim() &&
      selected.qc_cloud_backtest_id?.trim() &&
      selected.audit_copy_ref?.trim()
    ) {
      return null;
    }
    return 'The approved catalog entry is missing deploy evidence. Re-run Strategy Validation before launching it.';
  });

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

  readonly brokerAccountState = computed<'checking' | 'connected' | 'unavailable'>(() => {
    if (this.account.isLoading()) return 'checking';
    return this.brokerAccountAvailable() ? 'connected' : 'unavailable';
  });

  private readonly brokerAccountId = computed<string>(
    () => (this.account.hasValue() ? (this.account.value()?.account_id ?? '') : ''),
  );

  readonly readonlyFlag = computed<boolean>(() => this.executionMode() === 'read_only');
  readonly executionCapability = computed<ExecutionMode>(() => this.executionMode());
  readonly dailyOrderLimitError = computed<string | null>(() => {
    const value = this.maxOrdersPerDay();
    if (!Number.isInteger(value) || value < 0 || value > 100_000) {
      return 'Enter a whole number from 0 to 100,000. The server receives this exact daily limit.';
    }
    return null;
  });

  readonly dailyOrderLimitLabel = computed<string>(() => {
    const value = this.maxOrdersPerDay();
    return Number.isFinite(value) ? `${value.toLocaleString()} orders/day` : 'Invalid limit';
  });

  readonly sizingSummary = computed<string>(() => {
    if (this.sizingPreset() === 'safe_canary') return 'One share per signal';
    if (this.sizingPreset() === 'custom') {
      return this.customKind() === 'FixedShares' ? 'Fixed shares' : 'Fixed notional';
    }
    return 'Reference parity';
  });

  readonly legCount = computed<number>(() => this.actionPlan().on_enter.length);

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
    if (evidence === null || this.identityCoherenceConfirmed()) {
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
  readonly exposureLaunchDecision = computed<ExposureLaunchDecision | null>(() =>
    buildExposureLaunchDecision(this.exposureCoherenceEvidence()),
  );
  readonly exposureCoherenceBlock = computed<ExposureCoherenceConflict | null>(() => {
    const evidence = this.exposureCoherenceEvidence();
    if (evidence === null || this.exposureCoherenceConfirmed()) {
      return null;
    }
    return evidence;
  });

  readonly deploymentNameError = computed<string | null>(() => {
    const id = this.instanceId();
    return id.trim() !== '' && !INSTANCE_ID_RE.test(id)
      ? 'Use letters, numbers, periods, underscores, or hyphens.'
      : null;
  });

  deploymentNameFieldError(): string | null {
    if (!this.ticketForm.instanceId().touched()) return null;
    return this.instanceId().trim() === '' ? 'Enter a deployment name.' : this.deploymentNameError();
  }

  strategyFieldError(): string | null {
    if (!this.ticketForm.strategyKey().touched()) return null;
    if (this.strategyKey().trim() === '') return 'Choose an approved strategy.';
    return this.validatedReceiptIssue();
  }

  signalStreamFieldError(): string | null {
    return this.ticketForm.signalStream().touched() && this.resolvedSignalStream() === ''
      ? 'Enter the market-data stream this strategy should read.'
      : null;
  }

  dailyOrderLimitFieldError(): string | null {
    return this.ticketForm.maxOrdersPerDay().touched() ? this.dailyOrderLimitError() : null;
  }

  customSizingFieldError(): string | null {
    if (this.sizingPreset() !== 'custom') return null;
    return this.ticketForm.customValue().touched() ? this.customSizingError() : null;
  }

  executionModeFieldError(): string | null {
    return this.executionModeTouched() && this.executionMode() === 'live'
      ? 'Live orders are unavailable here. Choose paper orders or read-only.'
      : null;
  }

  legsFieldError(): string | null {
    return this.legsTouched() && !this.actionPlanReadiness().canDeploy
      ? this.actionPlanReadiness().message
      : null;
  }

  readonly missingRequiredFields = computed<string[]>(() => {
    const missing: string[] = [];
    if (this.strategyKey().trim() === '') missing.push('Strategy');
    if (this.resolvedSignalStream() === '') missing.push('Signal stream');
    if (this.instanceId().trim() === '') missing.push('Deployment name');
    return missing;
  });

  readonly formBlockers = computed<OperatorBlocker[]>(() =>
    buildFormBlockers({
      missingRequiredFields: this.missingRequiredFields(),
      validationReceiptIssue: this.validatedReceiptIssue(),
      brokerAccountState: this.brokerAccountState(),
      deploymentNameError: this.deploymentNameError(),
      identityConflictSummary: this.identityCoherenceBlock()?.summary ?? null,
      exposureConflictSummary: this.exposureCoherenceBlock()?.summary ?? null,
      customSizingError: this.customSizingError(),
      dailyOrderLimitError: this.dailyOrderLimitError(),
      allInCoexistenceBlock: this.allInCoexistenceBlock(),
      liveExecutionSelected: this.executionMode() === 'live',
      actionPlanReady: this.actionPlanReadiness().canDeploy,
      actionPlanMessage: this.actionPlanReadiness().message,
    }),
  );

  readonly blockers = computed<OperatorBlocker[]>(() => [
    ...(this.visibleDeployPreflight()?.blockers ?? []),
    ...this.formBlockers(),
  ]);

  readonly ready = computed<boolean>(() => deployReady(this.blockers()));

  readonly topBlocker = computed<OperatorBlocker | null>(() => {
    const blocking = this.blockers().filter((b) => b.condition.severity === 'blocking');
    return blocking[0] ?? null;
  });

  handleBlockerMove(event: OperatorBlockerMoveEvent): void {
    resolveBlockerMove(event.move, {
      navigate: (route, fragment) =>
        void this.router.navigate([route], fragment ? { fragment } : {}),
      focusAnchor: (anchor) => this.focusDeployAnchor(anchor),
    })?.invoke();
  }
  constructor() {
    // Re-deploy URLs seed operator-owned choices; the selected validation
    // receipt remains the only source of deploy provenance.
    const prefill = deployPrefillParamsFromQuery(this.route.snapshot.queryParamMap);
    if (prefill.strategyKey) this.updateTicket({ strategyKey: prefill.strategyKey });
    if (prefill.instanceId) this.updateTicket({ instanceId: prefill.instanceId });
    if (prefill.inheritedSymbol) this.inheritedSymbol.set(prefill.inheritedSymbol);
    if (prefill.inheritedSymbolSource) this.inheritedSymbolSource.set(prefill.inheritedSymbolSource);
    if (prefill.inheritedExposurePosture) this.inheritedExposurePosture.set(prefill.inheritedExposurePosture);
    if (prefill.inheritedExposurePendingOrderCount !== null) {
      this.inheritedExposurePendingOrderCount.set(prefill.inheritedExposurePendingOrderCount);
    }
    this.inheritedExposurePositions.set(prefill.inheritedExposurePositions);
    if (prefill.inheritedExposureSource) this.inheritedExposureSource.set(prefill.inheritedExposureSource);
    if (prefill.parentRunId) this.parentRunId.set(prefill.parentRunId);
    if (prefill.actionPlan) this.actionPlan.set(prefill.actionPlan);
    if (prefill.signalStream) {
      this.signalStreamManuallyEdited.set(true);
      this.updateTicket({ signalStream: prefill.signalStream });
    }

    effect(() => {
      const validation = this.selectedValidation();
      if (!validation) return;
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
      if (!this.signalStreamManuallyEdited()) {
        this.updateTicket({ signalStream: validationSignal });
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
    effect(() => {
      const response = this.deployPreflight.value();
      const params = this.deployPreflightRequest();
      if (this.deployPreflight.isLoading() || response === null || params === null) return;
      this.settledDeployPreflight.set({ params, response });
    });
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
      'Flatten the position, or choose one share per signal / a fixed size — the capital-sleeve layer that would let ' +
      'two all-in bots coexist on one symbol is not built yet.'
    );
  });

  readonly canSubmit = computed<boolean>(
    () => this.ticketForm().valid() && this.ready() && this.commandState().canSubmit,
  );

  async submit(start = true): Promise<void> {
    if (!this.canSubmit()) {
      this.ticketForm.instanceId().markAsTouched();
      this.ticketForm.strategyKey().markAsTouched();
      this.ticketForm.signalStream().markAsTouched();
      this.ticketForm.maxOrdersPerDay().markAsTouched();
      this.ticketForm.customValue().markAsTouched();
      this.executionModeTouched.set(true);
      this.legsTouched.set(true);
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    this.activeCoherenceRecovery.set(null);
    this.deployed.set(null);
    this.deployedInstanceId.set(null);
    this.preparedForCohort.set(false);
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
      start,
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
    const maxOrders = this.maxOrdersPerDay();
    request.start_options = {
      readonly: this.readonlyFlag(),
      hydrate_policy: this.hydratePolicy(),
      strategy: strategyKey,
      max_orders_per_day: maxOrders,
      ibkr_host: '127.0.0.1',
    };
    try {
      const response = await this.svc.deployInstance(request);
      this.deployed.set(response);
      this.deployedInstanceId.set(request.strategy_instance_id);
      this.preparedForCohort.set(!start);
    } catch (err) {
      const identitySeeded = this.seedIdentityCoherenceEvidence(err);
      const exposureSeeded = this.seedExposureCoherenceEvidence(err);
      this.activeCoherenceRecovery.set(exposureSeeded ? 'exposure' : identitySeeded ? 'identity' : null);
      this.error.set(toOperationError('deploy', err));
    } finally {
      this.busy.set(false);
    }
  }

  async prepareForCohort(): Promise<void> {
    await this.submit(false);
  }

  private seedIdentityCoherenceEvidence(err: unknown): boolean {
    const seed = identityCoherenceSeedFromDeployError(err);
    if (seed === null) return false;
    this.inheritedSymbol.set(seed.inheritedSymbol);
    this.inheritedSymbolSource.set(seed.inheritedSymbolSource);
    this.identityCoherenceConfirmedSignature.set(null);
    return true;
  }

  private seedExposureCoherenceEvidence(err: unknown): boolean {
    const seed = exposureCoherenceSeedFromDeployError(err);
    if (seed === null) return false;
    this.inheritedExposurePosture.set(seed.posture);
    this.inheritedExposurePendingOrderCount.set(seed.pendingOrderCount);
    this.inheritedExposurePositions.set(seed.ownedPositions);
    this.inheritedExposureSource.set(seed.source);
    if (seed.parentRunId !== null) {
      this.parentRunId.set(seed.parentRunId);
    }
    this.exposureCoherenceConfirmedSignature.set(null);
    return true;
  }

  // Event readers that narrow without a type assertion.
  private text(e: Event): string {
    return e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement
      ? e.target.value
      : '';
  }

  updateTicket(patch: Partial<DeployTicketModel>): void {
    this.ticket.update((current) => ({ ...current, ...patch }));
  }

  private deployPreflightRequest(): DeployPreflightParams | null {
    const strategyKey = this.strategyKey().trim();
    const accountId = this.accountId().trim();
    const instanceId = this.instanceId().trim();
    if (strategyKey === '' || accountId === '') return null;
    return {
      strategyKey,
      accountId,
      instanceId: instanceId === '' ? '__unnamed__' : instanceId,
    };
  }

  setStrategyKey(e: Event): void {
    this.signalStreamManuallyEdited.set(false);
    this.updateTicket({ strategyKey: this.text(e) });
  }
  setSignalStream(e: Event): void {
    this.signalStreamManuallyEdited.set(true);
    this.updateTicket({ signalStream: normalizedSymbol(this.text(e)) });
  }
  setInstanceId(e: Event): void {
    this.updateTicket({ instanceId: this.text(e) });
  }
  setExecutionMode(e: Event): void {
    if (e.target instanceof HTMLInputElement) {
      this.executionModeTouched.set(true);
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
    if (e.target instanceof HTMLInputElement) {
      this.updateTicket({ maxOrdersPerDay: e.target.valueAsNumber });
    }
  }
  markLegsTouched(): void {
    this.legsTouched.set(true);
  }
  confirmIdentityCoherence(): void {
    const evidence = this.identityCoherenceEvidence();
    if (evidence !== null) {
      this.identityCoherenceConfirmedSignature.set(evidence.signature);
      this.clearCoherenceRecoveryError('identity');
    }
  }

  confirmExposureCoherence(): void {
    const evidence = this.exposureCoherenceEvidence();
    if (evidence !== null) {
      this.exposureCoherenceConfirmedSignature.set(evidence.signature);
      this.clearCoherenceRecoveryError('exposure');
    }
  }

  async confirmExposureAndDeployStart(): Promise<void> {
    if (this.busy()) return;
    this.confirmExposureCoherence();
    await this.submit();
  }

  private clearCoherenceRecoveryError(kind?: CoherenceRecoveryKind): void {
    const active = this.activeCoherenceRecovery();
    if (active === null) return;
    if (kind !== undefined && active !== kind) return;
    this.activeCoherenceRecovery.set(null);
    this.error.set(null);
  }

  private focusDeployAnchor(anchor: string): void {
    queueMicrotask(() => {
      this.host.nativeElement.querySelector<HTMLElement>(`#${anchor}`)?.scrollIntoView?.({ block: 'center' });
    });
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

  readonly customSizingError = computed<string | null>(() =>
    customSizingValidationMessage({
      preset: this.sizingPreset(),
      kind: this.customKind(),
      rawValue: this.customValue(),
    }),
  );

  private resolveSizingPolicy(): SizingPolicy {
    return resolveDeploySizingPolicy({
      sizingSurfaceIsExplicit: this.sizingSurfaceIsExplicit(),
      preset: this.sizingPreset(),
      customKind: this.customKind(),
      customValue: this.customValue(),
    });
  }

  setCustomKind(e: Event): void {
    if (!(e.target instanceof HTMLSelectElement || e.target instanceof HTMLInputElement)) return;
    const v = e.target.value;
    if (v === 'FixedShares' || v === 'FixedNotional') {
      // Re-default the value to a sane shape for the kind (1 share / 100 dollars).
      this.updateTicket({ customKind: v, customValue: v === 'FixedShares' ? '1' : '100' });
    }
  }

  setCustomValue(e: Event): void {
    this.updateTicket({ customValue: this.text(e) });
  }
}
