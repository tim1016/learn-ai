import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import {
  form,
  max,
  min,
  pattern,
  required,
  validate,
} from '@angular/forms/signals';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { TagModule } from 'primeng/tag';
import { TooltipModule } from 'primeng/tooltip';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type DeployBotStrategy,
  type DeployExecutionMode,
  type DeployStrategyParamsSchema,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployBindingStripComponent } from './deploy-binding-strip.component';
import {
  DeployExecutionSectionComponent,
  type DeploySizingPreset,
} from './deploy-execution-section.component';
import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import { DeployParametersSectionComponent } from './deploy-parameters-section.component';
import { DeployReadinessSectionComponent } from './deploy-readiness-section.component';
import { DeployStartAdmissionComponent } from './deploy-start-admission.component';

const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SYMBOL_RE = /^[A-Za-z][A-Za-z0-9.-]{0,11}$/;
const DEPLOY_LENS_QUERY_PARAM = 'deployLens';

interface AlpacaDeployTicket {
  instanceId: string;
  strategyKey: DeployBotStrategy['strategy_key'] | '';
  symbol: string;
  sizingPreset: 'safe_canary' | 'custom';
  quantity: number;
  executionMode: Extract<DeployExecutionMode['mode'], 'dry_run' | 'paper'>;
  allowCarryover: boolean;
  parameters: Record<string, unknown>;
}

interface DeployError {
  outcome: 'conflict' | 'blocked' | 'unknown';
  title: string;
  message: string;
  explanation: string | null;
  nextAction: string | null;
  receiptId: string | null;
  recordedAtMs: number | null;
}

interface DeploySubmissionReadiness {
  canSubmit: boolean;
  guidance: string;
}

type DeployLens = 'trader' | 'operator';

@Component({
  selector: 'app-alpaca-deploy-workflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    ButtonModule,
    TagModule,
    TooltipModule,
    TimestampDisplayComponent,
    DeployBindingStripComponent,
    DeployExecutionSectionComponent,
    DeployLaunchReceiptComponent,
    DeployParametersSectionComponent,
    DeployReadinessSectionComponent,
    DeployStartAdmissionComponent,
  ],
  templateUrl: './alpaca-deploy-workflow.component.html',
  styleUrl: './alpaca-deploy-workflow.component.scss',
})
export class AlpacaDeployWorkflowComponent {
  readonly accountId = input.required<string>();
  protected readonly operatorLensQuery = { lens: 'operator' } as const;

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly activeLens = linkedSignal<DeployLens>(() =>
    (this.queryParams().get(DEPLOY_LENS_QUERY_PARAM) ?? this.queryParams().get('lens')) === 'operator'
      ? 'operator'
      : 'trader',
  );

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<DeployError | null>(null);
  protected readonly invalidParameterFields = signal<ReadonlySet<string>>(new Set());
  protected readonly receipt = signal<DeployBotReceipt | null>(null);
  protected readonly admissionDecision = signal<RunAdmissionDecision | null>(null);

  protected readonly deployView = resource({
    params: () => this.accountId().trim(),
    loader: ({ params }) => this.panelService.getDeployView('alpaca', params),
  });

  private readonly currentView = computed(() =>
    this.deployView.hasValue() ? this.deployView.value() : null,
  );

  protected readonly ticket = signal<AlpacaDeployTicket>({
    instanceId: '',
    strategyKey: '',
    symbol: '',
    sizingPreset: 'safe_canary',
    quantity: 1,
    executionMode: 'paper',
    allowCarryover: false,
    parameters: {},
  });

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

  // Informational only (#1702): the behavioral verdict renders in every
  // mode, but no longer gates Paper — a human-validated flag plus full
  // Clerk custody proof is enough. The backend's `admissible_modes` is the
  // sole authority on whether a mode is reachable; the frontend never
  // re-derives that from `evidence_status`.
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
      return {
        canSubmit: false,
        guidance: this.paperUnavailableReason() ?? 'This strategy is not admissible for the selected mode.',
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
    if (!this.ticketForm().valid()) {
      return { canSubmit: false, guidance: 'Complete the highlighted deployment fields.' };
    }
    return { canSubmit: true, guidance: 'Ready to deploy this bot.' };
  });

  protected readonly canSubmit = computed(() => this.submissionReadiness().canSubmit);

  constructor() {
    effect(() => {
      const view = this.currentView();
      const requestedKey = this.queryParams().get('strategy') ?? this.queryParams().get('strategy_key');
      const strategy = view?.strategies.find((candidate) => candidate.strategy_key === requestedKey)
        ?? view?.strategies.find((candidate) => candidate.selectable)
        ?? view?.strategies[0];
      if (!strategy) return;
      this.ticket.update((current) => {
        const strategyKey = current.strategyKey || strategy.strategy_key;
        const symbol = current.symbol || strategy.validation_case_symbol;
        if (strategyKey === current.strategyKey && symbol === current.symbol) return current;
        return { ...current, strategyKey, symbol };
      });
    });
  }

  protected selectLens(lens: DeployLens): void {
    this.activeLens.set(lens);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [DEPLOY_LENS_QUERY_PARAM]: lens },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected onLensKeydown(event: KeyboardEvent): void {
    const nextLens =
      event.key === 'ArrowRight' || event.key === 'End'
        ? 'operator'
        : event.key === 'ArrowLeft' || event.key === 'Home'
          ? 'trader'
          : null;
    if (nextLens === null) return;
    event.preventDefault();
    this.selectLens(nextLens);
    if (!(event.currentTarget instanceof HTMLElement)) return;
    const target = event.currentTarget.parentElement?.querySelector(`[data-lens="${nextLens}"]`);
    if (target instanceof HTMLElement) target.focus();
  }

  protected setInstanceId(value: string): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, instanceId: value.trim() }));
  }

  protected setStrategyKey(value: DeployBotStrategy['strategy_key']): void {
    const strategy = this.currentView()?.strategies.find((candidate) => candidate.strategy_key === value);
    if (!strategy) return;
    this.clearAdmission();
    this.ticket.update((current) => {
      const previous = this.currentView()?.strategies.find(
        (candidate) => candidate.strategy_key === current.strategyKey,
      );
      const symbol = current.symbol && current.symbol !== previous?.validation_case_symbol
        ? current.symbol
        : strategy.validation_case_symbol;
      return {
        ...current,
        strategyKey: strategy.strategy_key,
        symbol,
        parameters: {},
      };
    });
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
    this.ticket.update((current) => ({ ...current, symbol: value.trim().toUpperCase() }));
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
    // #1702: the evidence-only override contract no longer gates Paper — it
    // is re-pointed at Live, which this ticket cannot reach (`executionMode`
    // is closed to 'dry_run' | 'paper'). Never attach `evidence_override`:
    // the backend now rejects one submitted with a Paper request outright.
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
      && JSON.stringify(current.parameters) === JSON.stringify(submitted.parameters);
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
