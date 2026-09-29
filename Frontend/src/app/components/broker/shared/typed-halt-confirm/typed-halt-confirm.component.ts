import { DOCUMENT } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  ElementRef,
  EnvironmentInjector,
  inject,
  afterNextRender,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';

/**
 * Shared confirmation dialog for backend-authored broker actions.
 * An optional exact token preserves deliberate friction for destructive
 * actions; an empty token provides the same accessible surface for ordinary
 * confirmation.
 *
 * The dialog is open for exactly as long as it is mounted: every host renders
 * it under its own `@if` and removes it on `confirmed` or `cancelled`. So the
 * keyboard enters it when it is created and, after a cancel, goes back to
 * what opened it when it is destroyed (PRD #2560 story 80). A confirm leaves
 * the keyboard to its host, which moves it to the outcome.
 */
@Component({
  selector: 'app-typed-halt-confirm',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  host: {
    // Focus outside the dialog: Escape still cancels.
    '(document:keydown.escape)': 'onEscape()',
    // Clicks and keystrokes from the backdrop or the dialog stop at this
    // host, so the page behind it, including an enclosing menu that also
    // closes on Escape (the bot banner's overflow menu), never acts on them.
    // That hides Escape from the document listener too, so it cancels here.
    '(click)': '$event.stopPropagation()',
    '(keydown)': '$event.stopPropagation()',
    '(keydown.escape)': 'onEscape()',
  },
  templateUrl: './typed-halt-confirm.component.html',
  styleUrl: './typed-halt-confirm.component.scss',
})
export class TypedHaltConfirmComponent {
  /** Heading shown above the message. */
  readonly heading = input.required<string>();
  /** Body copy explaining what the action does.  Operator-language. */
  readonly message = input.required<string>();
  /** Optional tradeable instrument rendered through the shared identity control. */
  readonly assetSymbol = input<string | null>(null);
  /** Optional account this action acts on, shown as its number (ADR 0064;
   * #2188). A confirmation is one of the two surfaces where the number earns
   * its place, so a host that knows which account a consequential action will
   * hit threads it in here.
   *
   * Optional, and on the same footing as `assetSymbol` above: a *structured
   * identity fact* the host supplies beside the backend-authored prose, never
   * prose this component composes. Hosts whose backend copy already names the
   * account in its body — the `flatten_stop` / `retire` / `archive` family —
   * leave it unset rather than printing the number twice. */
  readonly accountId = input<string | null>(null);
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

  private readonly _document = inject(DOCUMENT);
  /** What had the keyboard when the dialog opened: a cancel hands it back. */
  private readonly _opener: HTMLElement | null;
  private _cancelled = false;

  private readonly _typed = signal<string>('');
  private readonly _input = viewChild<ElementRef<HTMLInputElement>>('tokenInput');
  private readonly _cancelButton = viewChild<ElementRef<HTMLButtonElement>>('cancelButton');

  readonly canConfirm = computed<boolean>(
    () => this.requiredToken() === '' || this._typed() === this.requiredToken(),
  );

  constructor() {
    const active = this._document.activeElement;
    this._opener = active instanceof HTMLElement ? active : null;
    // Focus the token input when present, otherwise (tokenless plain-confirm
    // mode) the Cancel control, so keyboard focus enters the dialog instead
    // of resting on the toolbar action behind the modal.
    afterNextRender({
      write: () => (this._input()?.nativeElement ?? this._cancelButton()?.nativeElement)?.focus(),
    });
    // Hand the keyboard back once the host's render settles, not mid-render:
    // a host may re-enable its opener only after removing the dialog (the
    // cohort drawer's Review). The app injector outlives this component,
    // whose own render hooks die with it.
    const appInjector = inject(EnvironmentInjector);
    inject(DestroyRef).onDestroy(() => {
      const opener = this._opener;
      if (!this._cancelled || opener === null) return;
      afterNextRender(
        { write: () => { if (opener.isConnected) opener.focus(); } },
        { injector: appInjector },
      );
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

  /** Cancel; the host then removes the dialog, and its destruction hands
   * the keyboard back to whatever opened it. */
  onCancel(): void {
    this._cancelled = true;
    this.cancelled.emit(undefined);
  }

  onEscape(): void {
    this.onCancel();
  }
}
