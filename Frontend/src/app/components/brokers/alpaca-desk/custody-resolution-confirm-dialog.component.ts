import { ChangeDetectionStrategy, Component, ElementRef, computed, effect, input, output, signal, viewChild } from '@angular/core';
import { Textarea } from 'primeng/textarea';

import type { CustodyDiagnosis, CustodyDivergence } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { CustodyDivergenceComponent } from './custody-divergence.component';

const CONFIRMATION_TOKEN = 'RESOLVE';

/**
 * Required-comment + typed-token confirm dialog for Alpaca custody
 * resolution (Slice 2, Task 2.3). Presentational only — the Accounts-page
 * card (Task 2.4) opens this dialog and wires `confirmed` to the
 * `POST /clerk/resolve` endpoint; this component never calls it.
 *
 * Kept separate from `PanelActionCommentConfirmComponent` rather than merged
 * into it: that dialog is generic panel-action-confirmation copy with no
 * divergence data at all, while this one is `CustodyDiagnosis`-specific and
 * renders a divergence list plus a resolution plan. Different data shapes,
 * a deliberate scope decision rather than an oversight.
 */
@Component({
  selector: 'app-custody-resolution-confirm-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, Textarea, CustodyDivergenceComponent],
  templateUrl: './custody-resolution-confirm-dialog.component.html',
  styleUrl: './custody-resolution-confirm-dialog.component.scss',
})
export class CustodyResolutionConfirmDialogComponent {
  readonly open = input.required<boolean>();
  readonly diagnosis = input.required<CustodyDiagnosis>();
  readonly busy = input(false);
  readonly errorMessage = input<string | null>(null);
  readonly confirmed = output<{ reason: string }>();
  readonly cancelled = output();

  readonly reason = signal('');
  readonly token = signal('');

  private readonly dialog = viewChild<ElementRef<HTMLDialogElement>>('dialog');

  readonly canConfirm = computed(
    () => this.reason().trim().length > 0 && this.token() === CONFIRMATION_TOKEN,
  );

  // `divergences` / `resolution_plan` are optional (`?:`) on the generated
  // CustodyDiagnosis schema (default `[]` server-side); normalize here so the
  // template never has to reason about `undefined`.
  protected readonly divergences = computed<CustodyDivergence[]>(
    () => this.diagnosis().divergences ?? [],
  );
  protected readonly resolutionPlan = computed(() => this.diagnosis().resolution_plan ?? []);

  constructor() {
    effect(() => {
      const dialog = this.dialog()?.nativeElement;
      if (dialog === undefined) return;
      if (this.open() && !dialog.open && typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else if (!this.open() && dialog.open) {
        dialog.close();
      }
    });

    // A fresh open must never carry over a prior divergence's typed reason or
    // confirmation token — both are part of the audited recovery record.
    effect(() => {
      if (this.open()) {
        this.reason.set('');
        this.token.set('');
      }
    });
  }

  cancel(event?: Event): void {
    event?.preventDefault();
    if (!this.busy()) this.cancelled.emit();
  }

  confirm(): void {
    if (this.busy() || !this.canConfirm()) return;
    this.confirmed.emit({ reason: this.reason() });
  }

  updateReason(event: Event): void {
    const input = event.target;
    if (input instanceof HTMLTextAreaElement) this.reason.set(input.value);
  }

  updateToken(event: Event): void {
    const input = event.target;
    if (input instanceof HTMLInputElement) this.token.set(input.value);
  }
}
