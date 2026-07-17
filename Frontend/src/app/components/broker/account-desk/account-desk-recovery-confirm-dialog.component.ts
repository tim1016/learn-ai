import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, input, output, viewChild } from '@angular/core';
import { Textarea } from 'primeng/textarea';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { AccountDeskRecoveryConfirmation } from './account-desk-recovery-store.service';

/** Native-dialog confirmation surface for the existing account recovery endpoints. */
@Component({
  selector: 'app-account-desk-recovery-confirm-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, Textarea, TimestampDisplayComponent],
  templateUrl: './account-desk-recovery-confirm-dialog.component.html',
  styleUrl: './account-desk-recovery-confirm-dialog.component.scss',
})
export class AccountDeskRecoveryConfirmDialogComponent {
  readonly confirmation = input<AccountDeskRecoveryConfirmation | null>(null);
  readonly busy = input(false);
  readonly errorMessage = input<string | null>(null);
  readonly cancelled = output();
  readonly confirmed = output();
  readonly exposureReasonChanged = output<string>();
  readonly confirmationTokenChanged = output<string>();
  private readonly dialog = viewChild<ElementRef<HTMLDialogElement>>('dialog');
  readonly canConfirm = computed(() => {
    const confirmation = this.confirmation();
    return confirmation !== null &&
      (confirmation.requiredToken === '' || confirmation.providedToken === confirmation.requiredToken) &&
      (confirmation.command !== 'exposure_override' || confirmation.reason.trim().length > 0);
  });

  constructor() {
    effect(() => {
      const dialog = this.dialog()?.nativeElement;
      if (dialog === undefined) return;
      if (this.confirmation() !== null && !dialog.open && typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else if (this.confirmation() === null && dialog.open) {
        dialog.close();
      }
    });
  }

  cancel(event?: Event): void {
    event?.preventDefault();
    if (!this.busy()) this.cancelled.emit();
  }

  confirm(): void {
    const confirmation = this.confirmation();
    if (confirmation === null || this.busy()) return;
    if (this.canConfirm()) this.confirmed.emit();
  }

  updateReason(event: Event): void {
    const input = event.target;
    if (input instanceof HTMLTextAreaElement) this.exposureReasonChanged.emit(input.value);
  }

  updateToken(event: Event): void {
    const input = event.target;
    if (input instanceof HTMLInputElement) this.confirmationTokenChanged.emit(input.value);
  }
}
