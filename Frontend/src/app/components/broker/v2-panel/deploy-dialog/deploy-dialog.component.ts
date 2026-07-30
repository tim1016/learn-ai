import {
  ChangeDetectionStrategy,
  Component,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { CheckboxModule } from 'primeng/checkbox';
import { DialogModule } from 'primeng/dialog';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';

import { BrokerV2PanelService } from '../broker-v2-panel.service';

/**
 * Deploy-bot dialog. Collects strategy_instance_id, symbol, and rth_only
 * then calls deployBot(). On success emits `deployed` so the parent reloads
 * the catalog.
 */
@Component({
  selector: 'app-deploy-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    DialogModule,
    InputTextModule,
    ButtonModule,
    CheckboxModule,
    MessageModule,
  ],
  templateUrl: './deploy-dialog.component.html',
  styleUrl: './deploy-dialog.component.scss',
})
export class DeployDialogComponent {
  readonly broker = input.required<string>();
  readonly visible = input(false);
  readonly visibleChange = output<boolean>();
  readonly deployed = output();

  private readonly panelService = inject(BrokerV2PanelService);

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<string | null>(null);

  protected readonly form = new FormGroup({
    strategy_instance_id: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    symbol: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    rth_only: new FormControl(true, { nonNullable: true }),
  });

  protected close(): void {
    this.form.reset({ rth_only: true });
    this.submitError.set(null);
    this.visibleChange.emit(false);
  }

  protected async submit(): Promise<void> {
    if (this.form.invalid || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    const { strategy_instance_id, symbol, rth_only } = this.form.getRawValue();
    try {
      await this.panelService.deployBot(this.broker(), {
        strategy_instance_id,
        symbol: symbol.toUpperCase(),
        rth_only,
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
