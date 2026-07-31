import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  resource,
  signal,
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { InputNumberModule } from 'primeng/inputnumber';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { SelectModule } from 'primeng/select';

import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
} from '../lib/broker-v2-panel.service';

interface DeployError {
  message: string;
  explanation: string | null;
  nextAction: string | null;
}

/**
 * Closed Alpaca paper deployment workflow.
 *
 * The backend authors account posture, strategy/sizing choices, eligibility,
 * ActionPlan explanation, terminal receipt, and remedies. Angular selects from
 * that contract and never exposes log-only or broker/lifecycle semantics.
 */
@Component({
  selector: 'app-deploy-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    DialogModule,
    InputTextModule,
    InputNumberModule,
    SelectModule,
    ButtonModule,
    MessageModule,
  ],
  templateUrl: './deploy-dialog.component.html',
  styleUrl: './deploy-dialog.component.scss',
})
export class DeployDialogComponent {
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  readonly visible = input(false);
  readonly visibleChange = output<boolean>();
  readonly deployed = output();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly router = inject(Router);

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<DeployError | null>(null);
  protected readonly receipt = signal<DeployBotReceipt | null>(null);

  protected readonly deployView = resource({
    params: () => this.visible()
      ? { broker: this.broker(), accountId: this.accountId() }
      : undefined,
    loader: ({ params }) =>
      this.panelService.getDeployView(params.broker, params.accountId),
  });

  protected readonly form = new FormGroup({
    account_id: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    strategy_instance_id: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    strategy_key: new FormControl<'deployment_validation'>(
      'deployment_validation',
      { nonNullable: true, validators: [Validators.required] },
    ),
    symbol: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    sizing_preset: new FormControl<'safe_canary' | 'custom'>(
      'safe_canary',
      { nonNullable: true, validators: [Validators.required] },
    ),
    quantity: new FormControl(1, {
      nonNullable: true,
      validators: [Validators.required, Validators.min(1), Validators.max(100)],
    }),
  });

  private readonly sizingPreset = toSignal(
    this.form.controls.sizing_preset.valueChanges,
    { initialValue: this.form.controls.sizing_preset.value },
  );

  protected readonly customSizing = computed(
    () => this.sizingPreset() === 'custom',
  );

  constructor() {
    effect(() => {
      const view = this.deployView.value();
      if (!view) return;
      this.form.controls.account_id.setValue(view.account_id);
      const strategy = view.strategies[0];
      if (strategy) this.form.controls.strategy_key.setValue(strategy.strategy_key);
    });
  }

  protected close(): void {
    this.form.reset({
      account_id: '',
      strategy_instance_id: '',
      strategy_key: 'deployment_validation',
      symbol: '',
      sizing_preset: 'safe_canary',
      quantity: 1,
    });
    this.submitError.set(null);
    this.receipt.set(null);
    this.visibleChange.emit(false);
  }

  protected sizingChanged(): void {
    if (!this.customSizing()) {
      this.form.controls.quantity.setValue(1);
    }
  }

  protected async submit(): Promise<void> {
    const view = this.deployView.value();
    if (
      !view
      || !view.eligibility.eligible
      || !view.allowed_actions.includes('deploy')
      || this.form.invalid
      || this.submitting()
    ) {
      return;
    }
    this.submitting.set(true);
    this.submitError.set(null);
    const value = this.form.getRawValue();
    const body = {
      strategy_instance_id: value.strategy_instance_id,
      strategy_key: value.strategy_key,
      symbol: value.symbol.toUpperCase(),
      sizing: {
        preset: value.sizing_preset,
        quantity: value.sizing_preset === 'safe_canary' ? 1 : value.quantity,
      },
    } satisfies DeployBotBody;
    try {
      const receipt = await this.panelService.deployBot(
        this.broker(),
        value.account_id,
        body,
      );
      this.receipt.set(receipt);
      this.deployed.emit();
    } catch (error) {
      this.submitError.set(this.deployError(error));
    } finally {
      this.submitting.set(false);
    }
  }

  protected openPanel(): void {
    const panelPath = this.receipt()?.panel_path;
    if (panelPath) void this.router.navigateByUrl(panelPath);
  }

  private deployError(error: unknown): DeployError {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail as {
        message?: string;
        why?: string | null;
        next_action?: string | null;
      } | undefined;
      if (detail?.message) {
        return {
          message: detail.message,
          explanation: detail.why ?? null,
          nextAction: detail.next_action ?? null,
        };
      }
    }
    return {
      message: 'The deployment service did not return an authored receipt.',
      explanation: 'The request may not have reached the Alpaca paper control boundary.',
      nextAction: 'Check data-plane connectivity, then reload the deployment contract.',
    };
  }
}
