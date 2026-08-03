import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { Textarea } from 'primeng/textarea';

/**
 * Required-comment + typed-token confirm dialog for Operator-lens mutating
 * gate actions (`clear_hold`, `record_inventory_baseline` — Slice 3 comment
 * parity, Task 3.2). Co-located with `PanelActionButtonComponent`, the only
 * component that opens it; presentational only, it never calls the action
 * endpoint itself.
 *
 * Modeled on two existing precedents rather than invented from scratch:
 * - `TypedHaltConfirmComponent` for the token-gating `canConfirm` formula.
 * - `CustodyResolutionConfirmDialogComponent` for the native `<dialog>`
 *   shape (ESC is handled natively via the dialog's own `cancel` event —
 *   no separate document-level listener needed, same as that precedent),
 *   the required-comment textarea, and — critically — the
 *   reset-on-every-open effect that clears `reason`/`token` each time
 *   `open()` flips true, not just on first open (the bug class Task 2.4's
 *   fix round caught elsewhere on this branch).
 */
@Component({
  selector: 'app-panel-action-comment-confirm',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Textarea],
  templateUrl: './panel-action-comment-confirm.component.html',
  styleUrl: './panel-action-comment-confirm.component.scss',
})
export class PanelActionCommentConfirmComponent {
  /** When `true` the dialog is open; toggle to `false` on confirm or cancel. */
  readonly open = input.required<boolean>();
  /** Heading shown above the message. Backend-authored confirmation copy. */
  readonly heading = input.required<string>();
  /** Body copy explaining what the action does. Backend-authored. */
  readonly message = input.required<string>();
  /** Explicit consequence copy authored by the backend. */
  readonly consequence = input.required<string>();
  /** Token the operator must type to enable confirm, in addition to the
   *  required comment. An empty string disables the typing gate entirely. */
  readonly requiredToken = input<string>('');
  /** Confirm button label, backend-authored. */
  readonly confirmLabel = input.required<string>();

  readonly confirmed = output<{ reason: string }>();
  readonly cancelled = output();

  readonly reason = signal('');
  readonly token = signal('');

  private readonly dialog = viewChild<ElementRef<HTMLDialogElement>>('dialog');

  readonly canConfirm = computed(
    () =>
      this.reason().trim().length > 0 &&
      (this.requiredToken() === '' || this.token() === this.requiredToken()),
  );

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

    // A fresh open must never carry over a prior action's typed reason or
    // confirmation token — both are part of the audited journal record.
    effect(() => {
      if (this.open()) {
        this.reason.set('');
        this.token.set('');
      }
    });
  }

  cancel(event?: Event): void {
    event?.preventDefault();
    this.cancelled.emit();
  }

  confirm(): void {
    if (!this.canConfirm()) return;
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
