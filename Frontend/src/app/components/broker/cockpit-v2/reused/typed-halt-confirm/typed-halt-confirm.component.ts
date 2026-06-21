import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  ElementRef,
  HostListener,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

/**
 * Typed-HALT confirm dialog (PRD #607 / Slice 6 / #613).
 *
 * Replaces ``window.prompt('… Type HALT to confirm.')`` for the
 * irreversible MARK_POISONED operation.  The confirm button stays
 * disabled until the operator types the exact token (case-sensitive
 * ``HALT``); ESC and the Cancel affordance both leave state
 * unchanged.  Preserves the deliberate friction gate from
 * ``broker-instances.component.ts:407–411`` while killing the
 * ``window.prompt`` call.
 *
 * Kept intentionally small (a styled ``<dialog>`` element rather than
 * a full PrimeNG ``p-confirmDialog`` wrapper) so it can ship in this
 * slice without dragging the whole dialog-mount infrastructure with
 * it.  Slice 8 will fold both the friction-gated and plain confirm
 * dialogs into a single token-controlled surface.
 */
@Component({
  selector: 'app-typed-halt-confirm',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './typed-halt-confirm.component.html',
  styleUrl: './typed-halt-confirm.component.scss',
})
export class TypedHaltConfirmComponent {
  /** When ``true`` the dialog is open; toggle to ``false`` on confirm or cancel. */
  readonly open = input.required<boolean>();
  /** Heading shown above the message. */
  readonly heading = input<string>('Mark this run POISONED');
  /** Body copy explaining what the action does.  Operator-language. */
  readonly message = input<string>(
    'Flagging this instance as POISONED is IRREVERSIBLE: the current run can never resume on its run_id. Recovery requires a fresh deployment (new run_id) after you reconcile the account.',
  );
  /** Token the operator must type to enable the confirm button. */
  readonly requiredToken = input<string>('HALT');

  readonly confirmed = output();
  readonly cancelled = output();

  private readonly _typed = signal<string>('');
  private readonly _input = viewChild<ElementRef<HTMLInputElement>>('tokenInput');

  readonly canConfirm = computed<boolean>(() => this._typed() === this.requiredToken());

  constructor() {
    inject(ElementRef); // ensure DI parity with other dialog components
    effect(() => {
      // Reset the typed field whenever the dialog re-opens so a prior
      // value cannot bleed into a fresh confirmation flow.
      if (this.open()) {
        this._typed.set('');
        queueMicrotask(() => this._input()?.nativeElement.focus());
      }
    });
  }

  onTyped(value: string): void {
    this._typed.set(value);
  }

  onConfirm(): void {
    if (!this.canConfirm()) return;
    this.confirmed.emit(undefined);
  }

  onCancel(): void {
    this.cancelled.emit(undefined);
  }

  @HostListener('document:keydown.escape')
  onEscape(): void {
    if (this.open()) {
      this.onCancel();
    }
  }
}
