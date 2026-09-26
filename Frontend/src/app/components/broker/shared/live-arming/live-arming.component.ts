import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, signal } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';
import { DialogModule } from 'primeng/dialog';

import type { ResourceTarget } from '../../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { LiveArmingService, type ArmingPlan, type ArmingStatus } from './live-arming.service';

@Component({
  selector: 'app-live-arming',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DecimalPipe, FormField, DialogModule, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './live-arming.component.html',
})
export class LiveArmingComponent {
  readonly target = input.required<ResourceTarget>();
  readonly sid = input.required<string>();
  readonly changed = output();
  private readonly service = inject(LiveArmingService);
  private readonly verdicts = inject(AlpacaLiveVerdictService);
  private readonly identity = computed(() => JSON.stringify([this.target().clerkId, this.target().accountId,
    this.target().bindingGeneration, this.target().routingEpoch, this.sid()]));
  private readonly applied = signal<{ key: string; status: ArmingStatus } | null>(null);
  protected readonly state = resource({
    params: () => this.identity(),
    loader: () => this.service.status(this.target(), this.sid()),
  });
  protected readonly status = computed(() => (this.applied()?.key === this.identity() ? this.applied()?.status : null) ?? (this.state.hasValue() ? this.state.value() : null));
  protected readonly open = signal(false);
  protected readonly pending = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly plan = signal<ArmingPlan | null>(null);
  protected readonly confirmation = signal({ token: '' });
  protected readonly confirmationForm = form(this.confirmation);
  protected readonly canApply = computed(() => !this.pending() && this.plan() !== null
    && this.confirmation().token === this.plan()?.confirmation_token);
  private reviewedTarget: ResourceTarget | null = null;

  constructor() {
    effect(() => {
      this.identity();
      this.applied.set(null);
      this.open.set(false);
      this.plan.set(null);
      this.reviewedTarget = null;
      this.error.set(null);
      this.pending.set(false);
      this.confirmation.set({ token: '' });
    });
  }

  protected async review(): Promise<void> {
    this.open.set(true);
    this.plan.set(null);
    this.confirmation.set({ token: '' });
    this.error.set(null);
    this.pending.set(true);
    const key = this.identity();
    const target = this.target();
    this.reviewedTarget = target;
    try {
      const plan = await this.service.prepare(target, this.sid());
      if (this.identity() === key && this.open() && this.reviewedTarget === target) this.plan.set(plan);
    } catch (error) { if (this.identity() === key) this.showError(error); }
    finally { if (this.identity() === key) this.pending.set(false); }
  }

  protected close(): void {
    if (this.pending()) return;
    this.open.set(false);
    this.plan.set(null);
    this.reviewedTarget = null;
    this.confirmation.set({ token: '' });
  }

  protected async apply(): Promise<void> {
    const plan = this.plan();
    const target = this.reviewedTarget;
    if (plan === null || target === null || !this.canApply()) return;
    const key = this.identity();
    this.pending.set(true);
    this.error.set(null);
    try {
      await this.acceptStatus(await this.service.apply(target, plan.strategy_instance_id, plan, this.confirmation().token), key);
      if (this.identity() !== key) return;
      this.open.set(false);
      this.plan.set(null);
    } catch (error) { if (this.identity() === key) this.showError(error); }
    finally { if (this.identity() === key) this.pending.set(false); }
  }

  protected async disarm(): Promise<void> {
    const key = this.identity();
    this.pending.set(true);
    this.error.set(null);
    try { await this.acceptStatus(await this.service.disarm(this.target(), this.sid()), key); }
    catch (error) { if (this.identity() === key) this.showError(error); }
    finally { if (this.identity() === key) this.pending.set(false); }
  }

  private async acceptStatus(status: ArmingStatus, key: string): Promise<void> {
    if (this.identity() !== key) return;
    this.applied.set({ key, status });
    this.changed.emit();
    try { await this.verdicts.refresh(); }
    catch { if (this.identity() === key) this.error.set('The bot was updated. The account header could not refresh yet.'); }
  }

  private showError(error: unknown): void {
    this.error.set(error instanceof HttpErrorResponse && typeof error.error?.detail?.message === 'string'
      ? error.error.detail.message : 'The arming request could not be completed. Refresh and review a new plan.');
  }
}
