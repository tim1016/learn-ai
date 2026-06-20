import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  input,
  output,
  viewChild,
} from '@angular/core';

/**
 * Pre-trade checklist FAB (PRD #607 / Slice 7 / #614).
 *
 * Bottom-right floating action button that shows the failing-gate
 * count badge.  Emits ``toggleRequested`` on click; the parent
 * orchestrator owns the dialog's open/closed state and flips it.
 *
 * Focus restoration: when ``restoreFocus`` flips from ``true`` to a
 * fresh value (the parent's signal updated after the dialog closed),
 * the FAB grabs focus so keyboard operators are never stranded.
 */
@Component({
  selector: 'app-checklist-fab',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './checklist-fab.component.html',
  styleUrl: './checklist-fab.component.scss',
})
export class ChecklistFabComponent {
  readonly visible = input<boolean>(true);
  readonly failingCount = input<number>(0);
  readonly open = input<boolean>(false);
  /** Bumps when the parent wants the FAB to take focus back from the
   * dialog (one-shot focus restoration). */
  readonly restoreFocusTick = input<number>(0);

  readonly toggleRequested = output();

  private readonly btn = viewChild<ElementRef<HTMLButtonElement>>('fabBtn');

  constructor() {
    effect(() => {
      if (this.restoreFocusTick() > 0) {
        queueMicrotask(() => this.btn()?.nativeElement.focus());
      }
    });
  }

  onClick(): void {
    this.toggleRequested.emit(undefined);
  }
}
