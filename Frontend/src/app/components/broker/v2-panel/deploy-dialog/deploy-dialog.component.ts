import {
  ChangeDetectionStrategy,
  Component,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { CheckboxModule } from 'primeng/checkbox';
import { DialogModule } from 'primeng/dialog';
import { InputNumberModule } from 'primeng/inputnumber';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { SelectModule } from 'primeng/select';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';

/**
 * Deploy-bot dialog. Collects strategy_instance_id, symbol, rth_only, mode,
 * and quantity then calls deployBot(). On success emits `deployed` so the
 * parent reloads the catalog.
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
    CheckboxModule,
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

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<string | null>(null);

  protected readonly modeOptions = [
    { label: 'Log only (no orders)', value: 'log_only' as const },
    { label: 'Trade (submits real orders)', value: 'trade' as const },
  ];

  protected readonly form = new FormGroup({
    strategy_instance_id: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    symbol: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    use_rth: new FormControl(true, { nonNullable: true }),
    mode: new FormControl<'log_only' | 'trade'>('log_only', { nonNullable: true }),
    quantity: new FormControl(1, {
      nonNullable: true,
      validators: [Validators.required, Validators.min(1), Validators.max(100)],
    }),
  });

  // Track mode reactively via toSignal so @if in the template updates on change.
  private readonly modeValue = toSignal(this.form.controls.mode.valueChanges, {
    initialValue: 'log_only' as 'log_only' | 'trade',
  });

  protected get isTradeMode(): boolean {
    return this.modeValue() === 'trade';
  }

  constructor() {
    // Enable/disable quantity based on mode. Managed via the form API to avoid
    // the "disabled with reactive form" Angular warning.
    effect(() => {
      if (this.modeValue() === 'trade') {
        this.form.controls.quantity.enable();
      } else {
        this.form.controls.quantity.disable();
      }
    });
  }

  protected close(): void {
    this.form.reset({ use_rth: true, mode: 'log_only', quantity: 1 });
    this.submitError.set(null);
    this.visibleChange.emit(false);
  }

  protected async submit(): Promise<void> {
    if (this.form.invalid || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    const { strategy_instance_id, symbol, use_rth, mode, quantity } = this.form.getRawValue();
    try {
      await this.panelService.deployBot(this.broker(), this.accountId(), {
        strategy_instance_id,
        symbol: symbol.toUpperCase(),
        use_rth,
        mode,
        quantity,
      });
      this.deployed.emit();
      this.close();
    } catch {
      this.submitError.set('Deploy failed. Check the strategy ID and symbol, then try again.');
    } finally {
      this.submitting.set(false);
    }
  }
}
