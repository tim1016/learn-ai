import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
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
  validate,
} from '@angular/forms/signals';
import { RouterLink } from '@angular/router';

import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import { DeployReadinessSectionComponent } from './deploy-readiness-section.component';
import { DeployReviewSectionComponent } from './deploy-review-section.component';
import { DeployStartAdmissionComponent } from './deploy-start-admission.component';
import { DeployValidationEvidenceComponent } from './deploy-validation-evidence.component';

const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SYMBOL_RE = /^[A-Za-z][A-Za-z0-9.-]{0,11}$/;

interface AlpacaDeployTicket {
  instanceId: string;
  strategyKey: string;
  symbol: string;
  sizingPreset: 'safe_canary' | 'custom';
  quantity: number;
  allowCarryover: boolean;
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

@Component({
  selector: 'app-alpaca-deploy-workflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormField,
    RouterLink,
    TimestampDisplayComponent,
    DeployLaunchReceiptComponent,
    DeployReadinessSectionComponent,
    DeployReviewSectionComponent,
    DeployStartAdmissionComponent,
    DeployValidationEvidenceComponent,
  ],
  templateUrl: './alpaca-deploy-workflow.component.html',
  styleUrl: './alpaca-deploy-workflow.component.scss',
})
export class AlpacaDeployWorkflowComponent {
  readonly accountId = input.required<string>();

  private readonly panelService = inject(BrokerV2PanelService);

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<DeployError | null>(null);
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
    allowCarryover: false,
  });

  protected readonly ticketForm = form(this.ticket, (ticket) => {
    required(ticket.instanceId, { message: 'Enter a deployment name.' });
    pattern(ticket.instanceId, INSTANCE_ID_RE, {
      message: 'Use letters, numbers, periods, underscores, or hyphens.',
    });
    required(ticket.strategyKey, { message: 'Choose an accepted strategy.' });
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

  protected readonly selectedSizing = computed(() => {
    const preset = this.ticket().sizingPreset;
    return this.currentView()?.sizing_options.find(
      (option) => option.preset === preset,
    ) ?? null;
  });

  protected readonly effectiveQuantity = computed(() =>
    this.ticket().sizingPreset === 'safe_canary' ? 1 : this.ticket().quantity,
  );

  protected readonly sizingLabel = computed(() =>
    this.selectedSizing()?.label ?? 'Choose sizing',
  );

  protected readonly canSubmit = computed(() => {
    const view = this.currentView();
    return Boolean(
      view
      && view.eligibility.eligible
      && view.allowed_actions.includes('deploy')
      && this.ticketForm().valid()
      && !this.submitting(),
    );
  });

  constructor() {
    effect(() => {
      const strategy = this.currentView()?.strategies[0];
      if (!strategy) return;
      this.ticket.update((current) => {
        const strategyKey = current.strategyKey || strategy.strategy_key;
        const symbol = current.symbol || strategy.validation_case_symbol;
        if (strategyKey === current.strategyKey && symbol === current.symbol) return current;
        return { ...current, strategyKey, symbol };
      });
    });
  }

  protected setSizingPreset(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    if (value !== 'safe_canary' && value !== 'custom') return;
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      sizingPreset: value,
      quantity: value === 'safe_canary' ? 1 : current.quantity,
    }));
  }

  protected setCarryover(event: Event): void {
    if (!this.currentView()?.carryover_available) return;
    this.clearAdmission();
    const checked = (event.target as HTMLInputElement).checked;
    this.ticket.update((current) => ({ ...current, allowCarryover: checked }));
  }

  protected normalizeInstanceId(event: Event): void {
    this.normalizeTextInput(event, 'instanceId', (value) => value.trim());
  }

  protected normalizeSymbol(event: Event): void {
    this.normalizeTextInput(event, 'symbol', (value) => value.trim().toUpperCase());
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
    if (!view || !this.canSubmit()) return;

    this.submitting.set(true);
    this.submitError.set(null);
    this.admissionDecision.set(null);
    const ticket = this.ticket();
    const body = this.deployBody(ticket);

    try {
      const decision = await this.panelService.previewStartAdmission(
        'alpaca',
        view.account_id,
        body,
      );
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

  private deployBody(ticket: AlpacaDeployTicket): DeployBotBody {
    return {
      strategy_instance_id: ticket.instanceId.trim(),
      strategy_key: 'deployment_validation',
      symbol: ticket.symbol.trim().toUpperCase(),
      sizing: {
        preset: ticket.sizingPreset,
        quantity: this.effectiveQuantity(),
      },
      carryover_policy: ticket.allowCarryover ? 'ALLOW' : 'FORBID',
    };
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

  private normalizeTextInput(
    event: Event,
    field: 'instanceId' | 'symbol',
    normalize: (value: string) => string,
  ): void {
    const input = event.target as HTMLInputElement;
    const normalized = normalize(input.value);
    input.value = normalized;
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, [field]: normalized }));
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
