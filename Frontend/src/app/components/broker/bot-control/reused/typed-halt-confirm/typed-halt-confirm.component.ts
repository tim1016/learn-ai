import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  ElementRef,
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
 * ``Bot Control.component.ts:407–411`` while killing the
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
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  templateUrl: './typed-halt-confirm.component.html',
  styleUrl: './typed-halt-confirm.component.scss',
})
export class TypedHaltConfirmComponent {
  /** When ``true`` the dialog is open; toggle to ``false`` on confirm or cancel. */
  readonly open = input.required<boolean>();
  /** Heading shown above the message. */
  readonly heading = input.required<string>();
  /** Body copy explaining what the action does.  Operator-language. */
  readonly message = input.required<string>();
  /** Explicit consequence copy authored by the backend. */
  readonly consequence = input.required<string>();
  /** Token the operator must type to enable the confirm button. An empty
   *  string disables the typing gate entirely, turning this into a plain
   *  confirm dialog (the token field is hidden and confirm is always
   *  enabled) — the "plain confirm" surface anticipated above. */
  readonly requiredToken = input<string>('HALT');
  /** Confirm button label. Defaults to the poison verb for the
   *  friction-gated MARK_POISONED flow; plain confirms override it. */
  readonly confirmLabel = input.required<string>();

  readonly confirmed = output();
  readonly cancelled = output();

  private readonly _typed = signal<string>('');
  private readonly _input = viewChild<ElementRef<HTMLInputElement>>('tokenInput');
  private readonly _cancelButton = viewChild<ElementRef<HTMLButtonElement>>('cancelButton');

  readonly canConfirm = computed<boolean>(
    () => this.requiredToken() === '' || this._typed() === this.requiredToken(),
  );

  constructor() {
    inject(ElementRef); // ensure DI parity with other dialog components
    effect(() => {
      // Reset the typed field whenever the dialog re-opens so a prior
      // value cannot bleed into a fresh confirmation flow.
      if (this.open()) {
        this._typed.set('');
        // Focus the token input when present, otherwise (tokenless plain-confirm
        // mode) the Cancel control, so keyboard focus enters the dialog instead
        // of resting on the toolbar action behind the modal.
        queueMicrotask(() =>
          (this._input()?.nativeElement ?? this._cancelButton()?.nativeElement)?.focus(),
        );
      }
    });
  }

  onTyped(value: string): void {
    this._typed.set(value);
  }

  onTokenInput(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.onTyped(target.value);
    }
  }

  onConfirm(): void {
    if (!this.canConfirm()) return;
    this.confirmed.emit(undefined);
  }

  onCancel(): void {
    this.cancelled.emit(undefined);
  }

  onEscape(): void {
    if (this.open()) {
      this.onCancel();
    }
  }
}
