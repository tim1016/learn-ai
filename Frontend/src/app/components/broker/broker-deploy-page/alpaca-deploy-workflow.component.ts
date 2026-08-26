import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
  untracked,
} from '@angular/core';
import {
  form,
  max,
  min,
  pattern,
  required,
  validate,
} from '@angular/forms/signals';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';

import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type DeployBotStrategy,
  type DeployBotView,
  type DeployExecutionMode,
  type DeployStrategyParamsSchema,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployBindingStripComponent } from './deploy-binding-strip.component';
import {
  DeployExecutionSectionComponent,
  type DeploySizingPreset,
} from './deploy-execution-section.component';
import {
  DeployAdmissionColumnComponent,
  type DeployError,
} from './deploy-admission-column.component';
import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import { DeployParametersSectionComponent } from './deploy-parameters-section.component';
import { DeployPaperAccessComponent } from './deploy-paper-access.component';
import { DeployEvidenceOverrideComponent } from './deploy-evidence-override.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';

const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SYMBOL_RE = /^[A-Za-z][A-Za-z0-9.-]{0,11}$/;

/**
 * The symbol input fires per keystroke. Scoping the readiness fetch to every
 * intermediate prefix would burn a request per character and let a stale
 * response land after a newer one; one settle beat is enough.
 */
const SYMBOL_SCOPE_DEBOUNCE_MS = 400;

interface AlpacaDeployTicket {
  instanceId: string;
  strategyKey: DeployBotStrategy['strategy_key'] | '';
  symbol: string;
  sizingPreset: 'safe_canary' | 'custom';
  quantity: number;
  executionMode: Extract<DeployExecutionMode['mode'], 'dry_run' | 'paper'>;
  allowCarryover: boolean;
  parameters: Record<string, unknown>;
  overrideAcknowledged: boolean;
  overrideReason: string;
}

const OVERRIDE_REASON_MIN_LENGTH = 10;

interface DeploySubmissionReadiness {
  canSubmit: boolean;
  guidance: string;
}

@Component({
  selector: 'app-alpaca-deploy-workflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DeployAdmissionColumnComponent,
    DeployBindingStripComponent,
    DeployEvidenceOverrideComponent,
    DeployExecutionSectionComponent,
    DeployLaunchReceiptComponent,
    DeployPaperAccessComponent,
    DeployParametersSectionComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-deploy-workflow.component.html',
  styleUrl: './alpaca-deploy-workflow.component.scss',
})
export class AlpacaDeployWorkflowComponent {
  readonly accountId = input.required<string>();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  /** Still read for the `?strategy=` deep link; the lens param is gone. */
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<DeployError | null>(null);
  protected readonly invalidParameterFields = signal<ReadonlySet<string>>(new Set());
  protected readonly receipt = signal<DeployBotReceipt | null>(null);
  protected readonly admissionDecision = signal<RunAdmissionDecision | null>(null);

  /**
   * The symbol the readiness fetch is scoped to, or null for the
   * account-level view. Deliberately not a resource *dependency*: the ticket
   * is seeded from the loaded view, so keying the resource on the symbol
   * would loop (load → seed → reload → …) and strand the pane on its loading
   * state. Written only by `scheduleSymbolScope`, after the debounce and
   * after the guard that recognizes a symbol the gates already describe.
   */
  private readonly scopedSymbol = signal<string | null>(null);
  private symbolScopeTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * `params` stays keyed on the account alone. The scoped symbol is read
   * untracked inside the loader and applied by an explicit `reload()`, which
   * — unlike a `params` change — keeps the previous value on screen instead
   * of dropping it and flickering the pane back to its loading state.
   */
  protected readonly deployView = resource({
    params: () => this.accountId().trim(),
    loader: async ({ params, abortSignal }) => {
      const symbol = untracked(this.scopedSymbol);
      const view = await this.panelService.getDeployView(
        'alpaca',
        params,
        symbol ?? undefined,
      );
      // `getDeployView` wraps `firstValueFrom(http.get)`, which no `reload()`
      // can cancel, so a superseded request still answers — and, being
      // slower, can answer last. Letting its write win would leave the
      // retained record claiming a scope the operator has already left.
      if (!abortSignal.aborted) {
        this.lastLoadedView.set({ accountId: params, symbol, view });
      }
      return view;
    },
  });

  /**
   * The last readiness view that actually loaded, with the account AND symbol
   * it describes. Retaining it lets a failed *refresh* degrade to an explicit
   * staleness banner over the gates the operator already has, instead of
   * collapsing the pane — and the whole ticket with it — back to an error.
   *
   * The symbol is half the identity, not decoration. Without it a SPY-scoped
   * set of gates silently backed a QQQ ticket; with it, a mismatch is a
   * condition the pane can name and refuse to deploy on.
   */
  private readonly lastLoadedView = signal<{
    accountId: string;
    symbol: string | null;
    view: DeployBotView;
  } | null>(null);

  protected readonly currentView = computed(() => {
    if (this.deployView.hasValue()) return this.deployView.value();
    const retained = this.lastLoadedView();
    return retained?.accountId === this.accountId().trim() ? retained.view : null;
  });

  /**
   * True when what is on screen is not current admission truth for this
   * ticket: a refresh failed, or the gates that loaded describe a different
   * symbol than the one now scoped. Either way nothing may be deployed on
   * them — a `role="alert"` staleness banner beside a live Deploy button is
   * the worst of both.
   */
  private readonly admissionIsStale = computed(() => {
    const retained = this.lastLoadedView();
    if (retained === null) return false;
    return retained.symbol !== this.scopedSymbol() || this.deployView.error() !== undefined;
  });

  /**
   * The operator-facing half of the above: the sentence that names what is on
   * screen and what it is not. Silently serving stale admission truth is what
   * makes a deployment decision unsafe.
   */
  protected readonly stalenessNotice = computed<string | null>(() => {
    const retained = this.lastLoadedView();
    if (retained === null || this.currentView() === null) return null;
    if (!this.admissionIsStale()) return null;
    // A refresh still in flight is refreshing, not stale. Raising an alert
    // for every settled keystroke would teach the operator to ignore it;
    // `admissionIsStale` still refuses the deploy meanwhile.
    if (this.deployView.isLoading()) return null;
    const shown = retained.symbol ?? 'this account';
    const wanted = this.scopedSymbol() ?? 'this account';
    return wanted === shown
      ? `Deployment readiness for ${shown} could not be refreshed. ` +
        'The gates below are the last that loaded.'
      : `Deployment readiness for ${wanted} could not be refreshed. ` +
        `The gates below describe ${shown}.`;
  });

  protected readonly ticket = signal<AlpacaDeployTicket>({
    instanceId: '',
    strategyKey: '',
    symbol: '',
    sizingPreset: 'safe_canary',
    quantity: 1,
    executionMode: 'paper',
    allowCarryover: false,
    parameters: {},
    overrideAcknowledged: false,
    overrideReason: '',
  });

  private readonly overrideReasonTouched = signal(false);

  protected readonly ticketForm = form(this.ticket, (ticket) => {
    required(ticket.instanceId, { message: 'Enter a deployment name.' });
    pattern(ticket.instanceId, INSTANCE_ID_RE, {
      message: 'Use letters, numbers, periods, underscores, or hyphens.',
    });
    required(ticket.strategyKey, { message: 'Choose a deployment strategy.' });
    required(ticket.symbol, { message: 'Enter the strategy signal symbol.' });
    pattern(ticket.symbol, SYMBOL_RE, { message: 'Enter a valid stock symbol.' });
    min(ticket.quantity, 1, { message: 'Quantity must be at least one whole share.' });
    max(ticket.quantity, 100, { message: 'Quantity cannot exceed 100 shares.' });
    validate(ticket.quantity, ({ value }) =>
      Number.isInteger(value())
        ? undefined
        : { kind: 'whole-share-quantity', message: 'Quantity must be a whole number.' },
    );
  });

  protected readonly selectedStrategy = computed(() => {
    const strategyKey = this.ticket().strategyKey;
    return this.currentView()?.strategies.find(
      (strategy) => strategy.strategy_key === strategyKey,
    ) ?? null;
  });

  protected readonly paramsSchema = computed<DeployStrategyParamsSchema>(
    () => this.selectedStrategy()?.params_schema ?? {},
  );

  // A Paper deploy of an evidence-only strategy carries the durable human
  // override (acknowledgement + reason) on the request itself — restored by
  // operator decision 2026-08-24 after #1702/#1746 re-pointed it at Live.
  // The backend refuses an evidence-only Paper deploy without it, and
  // rejects one submitted for a fully accepted strategy.
  protected readonly overrideRequired = computed(() =>
    this.selectedStrategy()?.evidence_status === 'evidence_only'
      && this.ticket().executionMode === 'paper',
  );

  protected readonly overrideReasonError = computed(() => {
    if (!this.overrideRequired() || !this.overrideReasonTouched()) return null;
    return this.ticket().overrideReason.trim().length >= OVERRIDE_REASON_MIN_LENGTH
      ? null
      : 'Give at least 10 characters explaining why this risk is accepted.';
  });

  // The behavioral verdict renders in every mode. The backend's
  // `admissible_modes` is the sole authority on whether a mode is reachable;
  // the frontend never re-derives that from `evidence_status`.
  protected readonly evidenceSummaryLabel = computed(() => {
    switch (this.selectedStrategy()?.evidence_status) {
      case 'evidence_only':
        return 'Evidence only';
      case 'blocked':
        return 'Blocked';
      default:
        return 'Accepted';
    }
  });

  // Backend-authored reason the Paper option is unreachable for the
  // selected strategy (#1702). `null` whenever Paper is admissible or no
  // strategy is selected yet — every non-Paper-admissible row is a blocked
  // row today, so `blocked_explanation` is always present when this fires.
  protected readonly paperUnavailableReason = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null || strategy.admissible_modes.includes('paper')) return null;
    return strategy.blocked_explanation ?? null;
  });

  // Backend-authored reason the Dry Run option is unreachable (#1703). Only
  // a validated strategy with no registered runtime is ever Dry-Run-blocked
  // — every other row (accepted, evidence-only, or blocked on a stale
  // proof) stays Dry-Run-admissible regardless of validation state.
  protected readonly dryRunUnavailableReason = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null || strategy.admissible_modes.includes('dry_run')) return null;
    return strategy.blocked_explanation ?? null;
  });

  /**
   * The admission column's own headline. It replaces the separate eligibility
   * banner the lens split needed: with the gates always on screen there is one
   * place that says whether launch is admitted and how many gates back that up.
   */
  protected readonly readinessSummary = computed(() => {
    const view = this.currentView();
    if (view === null) return null;
    const checks = view.readiness_checks;
    const ready = checks.filter((check) => check.ready).length;
    return {
      label: view.eligibility.eligible ? 'Ready' : 'Blocked',
      counts: `${ready} of ${checks.length}`,
    };
  });

  protected readonly selectedExecutionMode = computed(() => {
    const view = this.currentView();
    return view?.execution_modes.find(
      (mode) => mode.mode === this.ticket().executionMode,
    ) ?? null;
  });

  protected readonly executionLabel = computed(() =>
    this.selectedExecutionMode()?.label ?? 'Broker-authored',
  );

  protected readonly effectiveQuantity = computed(() =>
    this.ticket().sizingPreset === 'safe_canary' ? 1 : this.ticket().quantity,
  );

  protected readonly quantityLabel = computed(() => {
    const quantity = this.effectiveQuantity();
    return `${quantity} ${quantity === 1 ? 'share' : 'shares'}`;
  });

  protected readonly submissionReadiness = computed<DeploySubmissionReadiness>(() => {
    const view = this.currentView();
    if (!view) {
      return { canSubmit: false, guidance: 'Loading deployment readiness…' };
    }
    if (this.admissionIsStale()) {
      return {
        canSubmit: false,
        guidance: 'Refresh deployment readiness before launch.',
      };
    }
    if (!view.eligibility.eligible || !view.allowed_actions.includes('deploy')) {
      return {
        canSubmit: false,
        guidance: view.eligibility.next_action || 'Resolve the current blocker before launch.',
      };
    }
    if (this.submitting()) {
      return { canSubmit: false, guidance: 'Deployment is in progress.' };
    }
    if (this.ticketForm.instanceId().invalid()) {
      return { canSubmit: false, guidance: 'Fix the bot name before deployment.' };
    }
    const selectedStrategy = this.selectedStrategy();
    if (selectedStrategy === null) {
      return { canSubmit: false, guidance: 'Choose a deployment strategy.' };
    }
    // Mode-aware, not strategy-wide (#1702): a blocked strategy is still
    // Dry-Run-admissible, so admissibility is checked against the ticket's
    // chosen mode, not `selectable` (which means "Paper-admissible" only).
    if (!selectedStrategy.admissible_modes.includes(this.ticket().executionMode)) {
      const reason = this.ticket().executionMode === 'paper'
        ? this.paperUnavailableReason()
        : this.dryRunUnavailableReason();
      return {
        canSubmit: false,
        guidance: reason ?? 'This strategy is not admissible for the selected mode.',
      };
    }
    if (this.selectedExecutionMode()?.availability !== 'available') {
      return { canSubmit: false, guidance: 'Choose an available execution mode.' };
    }
    if (this.ticketForm.symbol().invalid()) {
      return { canSubmit: false, guidance: 'Fix the trading symbol before deployment.' };
    }
    if (this.ticket().sizingPreset === 'custom' && this.ticketForm.quantity().invalid()) {
      return { canSubmit: false, guidance: 'Fix the position size before deployment.' };
    }
    if (this.invalidParameterFields().size > 0) {
      return { canSubmit: false, guidance: 'Fix the highlighted strategy parameter before deployment.' };
    }
    if (this.overrideRequired()) {
      if (!this.ticket().overrideAcknowledged) {
        return {
          canSubmit: false,
          guidance: 'Acknowledge the evidence-only deployment risk before launch.',
        };
      }
      if (this.ticket().overrideReason.trim().length < OVERRIDE_REASON_MIN_LENGTH) {
        return {
          canSubmit: false,
          guidance: 'Record the operator reason for the evidence-only override.',
        };
      }
    }
    if (!this.ticketForm().valid()) {
      return { canSubmit: false, guidance: 'Complete the highlighted deployment fields.' };
    }
    return { canSubmit: true, guidance: 'Ready to deploy this bot.' };
  });

  protected readonly canSubmit = computed(() => this.submissionReadiness().canSubmit);

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.symbolScopeTimer !== null) clearTimeout(this.symbolScopeTimer);
    });

    effect(() => {
      const view = this.currentView();
      const requestedKey = this.queryParams().get('strategy') ?? this.queryParams().get('strategy_key');
      const strategy = view?.strategies.find((candidate) => candidate.strategy_key === requestedKey)
        ?? view?.strategies.find((candidate) => candidate.selectable)
        ?? view?.strategies[0];
      if (!strategy) return;
      const current = untracked(this.ticket);
      const strategyKey = current.strategyKey || strategy.strategy_key;
      if (strategyKey !== current.strategyKey) {
        this.ticket.update((ticket) => ({ ...ticket, strategyKey }));
      }
      const symbol = current.symbol || strategy.validation_case_symbol;
      if (symbol !== current.symbol) this.applySymbol(symbol);
    });
  }

  protected setInstanceId(value: string): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, instanceId: value.trim() }));
  }

  protected setStrategyKey(value: DeployBotStrategy['strategy_key']): void {
    const strategy = this.currentView()?.strategies.find((candidate) => candidate.strategy_key === value);
    if (!strategy) return;
    this.clearAdmission();
    const current = this.ticket();
    const previous = this.currentView()?.strategies.find(
      (candidate) => candidate.strategy_key === current.strategyKey,
    );
    // An operator's own symbol survives a strategy switch; a symbol that was
    // only the previous strategy's validation case is replaced by the new
    // strategy's.
    const symbol = current.symbol && current.symbol !== previous?.validation_case_symbol
      ? current.symbol
      : strategy.validation_case_symbol;
    this.ticket.update((ticket) => ({
      ...ticket,
      strategyKey: strategy.strategy_key,
      parameters: {},
      overrideAcknowledged: false,
      overrideReason: '',
    }));
    this.applySymbol(symbol);
    this.overrideReasonTouched.set(false);
  }

  protected setOverrideAcknowledged(checked: boolean): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, overrideAcknowledged: checked }));
  }

  protected setOverrideReason(value: string): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, overrideReason: value }));
  }

  protected touchOverrideReason(): void {
    this.overrideReasonTouched.set(true);
  }

  protected setParameter(change: { field: string; value: string | number }): void {
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      parameters: { ...current.parameters, [change.field]: change.value },
    }));
  }

  protected setInvalidParameterFields(fields: ReadonlySet<string>): void {
    this.invalidParameterFields.set(fields);
  }

  protected setSymbol(value: string): void {
    this.clearAdmission();
    this.applySymbol(value.trim().toUpperCase());
  }

  /**
   * The single writer of the ticket symbol, and therefore the single place
   * readiness is re-scoped. All three paths that move the symbol go through
   * here: the view's seeding effect, a strategy switch, and the operator's
   * own typing.
   *
   * Two of the three used to write the ticket directly. That left the pane
   * showing ACCOUNT-level channel health under a populated symbol on first
   * paint — the scoping this work exists to wire, inactive exactly when it
   * was needed — and, after a strategy switch, symbol-A's gates under symbol
   * B, with `canSubmit` gating on them.
   *
   * The loop this used to be feared for stays closed downstream, in
   * `scheduleSymbolScope`: it stops as soon as the gates on screen already
   * describe the symbol being applied, which is what the returning view
   * re-seeds.
   */
  private applySymbol(symbol: string): void {
    this.ticket.update((current) =>
      current.symbol === symbol ? current : { ...current, symbol },
    );
    this.scheduleSymbolScope(symbol);
  }

  private scheduleSymbolScope(symbol: string): void {
    if (this.symbolScopeTimer !== null) clearTimeout(this.symbolScopeTimer);
    this.symbolScopeTimer = setTimeout(() => {
      this.symbolScopeTimer = null;
      // A half-typed ticker is not a scope. Wait for a symbol the broker
      // contract would actually accept rather than round-tripping a 422.
      if (!SYMBOL_RE.test(symbol)) return;
      // The gates that actually loaded already describe this symbol — the
      // condition that closes the seed loop, and the one that lets a failed
      // scope be retried (the requested scope alone cannot tell them apart).
      if (this.lastLoadedView()?.symbol === symbol) return;
      this.scopedSymbol.set(symbol);
      // A resource mid-load refuses `reload()`. Re-arm instead of dropping
      // the newer scope on the floor: dropped, it strands the pane on gates
      // for a symbol the operator has already left, with no way back.
      if (!this.deployView.reload()) this.scheduleSymbolScope(symbol);
    }, SYMBOL_SCOPE_DEBOUNCE_MS);
  }

  protected setSizingPreset(value: DeploySizingPreset): void {
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      sizingPreset: value,
      quantity: value === 'safe_canary' ? 1 : current.quantity,
    }));
  }

  protected setQuantity(quantity: number): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, quantity }));
  }

  protected setExecutionMode(mode: DeployExecutionMode['mode']): void {
    if (mode !== 'dry_run' && mode !== 'paper') return;
    const option = this.currentView()?.execution_modes.find(
      (candidate) => candidate.mode === mode,
    );
    if (option?.availability !== 'available') return;
    if (mode === 'paper' && this.paperUnavailableReason() !== null) return;
    if (mode === 'dry_run' && this.dryRunUnavailableReason() !== null) return;
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      executionMode: mode,
      allowCarryover: mode === 'dry_run' ? false : current.allowCarryover,
    }));
  }

  protected setCarryover(checked: boolean): void {
    if (this.ticket().executionMode === 'dry_run') return;
    if (!this.currentView()?.carryover_available) return;
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, allowCarryover: checked }));
  }

  protected touchInstanceId(): void {
    this.ticketForm.instanceId().markAsTouched();
  }

  protected touchSymbol(): void {
    this.ticketForm.symbol().markAsTouched();
  }

  protected touchQuantity(): void {
    this.ticketForm.quantity().markAsTouched();
  }

  protected reload(): void {
    this.submitError.set(null);
    this.admissionDecision.set(null);
    this.deployView.reload();
  }

  protected paperAccessChanged(): void {
    this.clearAdmission();
    this.deployView.reload();
  }

  protected clearAdmission(): void {
    this.admissionDecision.set(null);
  }

  protected async submit(): Promise<void> {
    this.markFormTouched();
    const view = this.currentView();
    const strategy = this.selectedStrategy();
    if (!view || !strategy || !this.canSubmit()) return;

    this.submitting.set(true);
    this.submitError.set(null);
    this.admissionDecision.set(null);
    const ticket = this.ticket();
    const body = this.deployBody(ticket, strategy);

    try {
      const decision = await this.panelService.previewStartAdmission(
        'alpaca',
        view.account_id,
        body,
      );
      if (!this.submissionStillCurrent(body)) return;
      this.admissionDecision.set(decision);
      if (!decision.allowed) return;
      this.receipt.set(await this.panelService.deployBot('alpaca', view.account_id, body));
    } catch (error) {
      const decision = this.admissionFromError(error);
      if (decision) this.admissionDecision.set(decision);
      this.submitError.set(this.toDeployError(error));
    } finally {
      this.submitting.set(false);
    }
  }

  private deployBody(
    ticket: AlpacaDeployTicket,
    strategy: DeployBotStrategy,
  ): DeployBotBody {
    const body: DeployBotBody = {
      strategy_instance_id: ticket.instanceId.trim(),
      strategy_key: strategy.strategy_key,
      symbol: ticket.symbol.trim().toUpperCase(),
      sizing: {
        preset: ticket.sizingPreset,
        quantity: ticket.sizingPreset === 'safe_canary' ? 1 : ticket.quantity,
      },
      execution_mode: ticket.executionMode,
      carryover_policy: ticket.allowCarryover ? 'ALLOW' : 'FORBID',
      // `parameters` genuinely varies by strategy (unlike `params_schema`,
      // which has one uniform shape typed at the OpenAPI boundary) — the
      // generated type narrows this dict[str, Any] request field to
      // `Record<string, never>`, the same pre-existing codegen limitation
      // noted in strategy-lab-runner.service.ts's own `params` construction.
      parameters: ticket.parameters as unknown as DeployBotBody['parameters'],
    };
    // The durable evidence-only override rides the Paper request (operator
    // decision 2026-08-24, restoring what #1702 re-pointed at Live). Only an
    // evidence-only strategy carries it — the backend rejects an override on
    // an accepted strategy as superfluous.
    if (strategy.evidence_status === 'evidence_only' && ticket.executionMode === 'paper') {
      body.evidence_override = {
        acknowledgement: 'I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK',
        reason: ticket.overrideReason.trim(),
      };
    }
    return body;
  }

  private submissionStillCurrent(submitted: DeployBotBody): boolean {
    const strategy = this.selectedStrategy();
    if (!strategy) return false;
    const current = this.deployBody(this.ticket(), strategy);
    return current.strategy_instance_id === submitted.strategy_instance_id
      && current.strategy_key === submitted.strategy_key
      && current.symbol === submitted.symbol
      && current.sizing?.preset === submitted.sizing?.preset
      && current.sizing?.quantity === submitted.sizing?.quantity
      && current.execution_mode === submitted.execution_mode
      && current.carryover_policy === submitted.carryover_policy
      && JSON.stringify(current.parameters) === JSON.stringify(submitted.parameters)
      && JSON.stringify(current.evidence_override ?? null)
        === JSON.stringify(submitted.evidence_override ?? null);
  }

  protected instanceIdError(): string | null {
    if (!this.ticketForm.instanceId().touched()) return null;
    return this.ticketForm.instanceId().errors()[0]?.message ?? null;
  }

  protected symbolError(): string | null {
    if (!this.ticketForm.symbol().touched()) return null;
    return this.ticketForm.symbol().errors()[0]?.message ?? null;
  }

  protected quantityError(): string | null {
    if (this.ticket().sizingPreset !== 'custom' || !this.ticketForm.quantity().touched()) {
      return null;
    }
    return this.ticketForm.quantity().errors()[0]?.message ?? null;
  }

  private admissionFromError(error: unknown): RunAdmissionDecision | null {
    if (!(error instanceof HttpErrorResponse)) return null;
    const admission = error.error?.detail?.admission as RunAdmissionDecision | undefined;
    return admission ?? null;
  }
  private markFormTouched(): void {
    this.ticketForm.instanceId().markAsTouched();
    this.ticketForm.strategyKey().markAsTouched();
    this.ticketForm.symbol().markAsTouched();
    this.ticketForm.quantity().markAsTouched();
  }

  private toDeployError(error: unknown): DeployError {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail as {
        outcome?: 'conflict' | 'blocked' | 'unknown';
        receipt_id?: string | null;
        recorded_at_ms?: number | null;
        message?: string;
        why?: string | null;
        next_action?: string | null;
      } | undefined;
      if (detail?.message) {
        const outcome = detail.outcome ?? (error.status === 409 ? 'conflict' : 'blocked');
        return {
          outcome,
          title: this.errorTitle(outcome),
          message: detail.message,
          explanation: detail.why ?? null,
          nextAction: detail.next_action ?? null,
          receiptId: detail.receipt_id ?? null,
          recordedAtMs: detail.recorded_at_ms ?? null,
        };
      }
    }
    return {
      outcome: 'unknown',
      title: 'Outcome unknown',
      message: 'The deployment service did not return an authored receipt.',
      explanation: 'The request may not have reached the Alpaca paper control boundary.',
      nextAction: 'Check data-plane connectivity, then reload current deployment readiness.',
      receiptId: null,
      recordedAtMs: null,
    };
  }

  private errorTitle(outcome: DeployError['outcome']): string {
    if (outcome === 'conflict') return 'State changed before launch';
    if (outcome === 'blocked') return 'Deployment blocked';
    return 'Outcome unknown';
  }
}
