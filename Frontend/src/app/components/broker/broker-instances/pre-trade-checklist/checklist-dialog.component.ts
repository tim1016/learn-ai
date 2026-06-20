import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  input,
  output,
  viewChild,
} from '@angular/core';

import type { FailingGateRow } from '../failing-gates';

/**
 * Pre-trade checklist dialog (PRD #607 / Slice 7 / #614).
 *
 * The dialog surface — retains the existing custom ``<aside
 * role="dialog">`` (NOT a PrimeNG p-dialog, per the original Q10
 * user decision).  Carries an ``aria-labelledby`` reference to a
 * stable title-element id so screen readers announce the dialog
 * name; initial focus moves to the first interactive element when
 * the dialog opens; ESC and the close affordance both emit
 * ``closeRequested``.
 */
@Component({
  selector: 'app-checklist-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './checklist-dialog.component.html',
  styleUrl: './checklist-dialog.component.scss',
})
export class ChecklistDialogComponent {
  readonly open = input.required<boolean>();
  readonly failingGates = input.required<FailingGateRow[]>();
  readonly acknowledged = input.required<ReadonlySet<string>>();

  readonly acknowledge = output<string>();
  readonly closeRequested = output();

  private readonly dialogEl = viewChild<ElementRef<HTMLElement>>('dialogEl');
  private readonly firstFocusable = viewChild<ElementRef<HTMLElement>>('firstFocusable');

  constructor() {
    effect(() => {
      if (!this.open()) return;
      // Move initial focus into the dialog.  ``firstFocusable`` is
      // the first acknowledge button when gates exist, otherwise the
      // close button — see the template.
      queueMicrotask(() => this.firstFocusable()?.nativeElement.focus());
    });
  }

  onAck(key: string): void {
    this.acknowledge.emit(key);
  }

  onClose(): void {
    this.closeRequested.emit(undefined);
  }
}
