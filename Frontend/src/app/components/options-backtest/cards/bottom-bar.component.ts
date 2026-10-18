import { afterNextRender, ChangeDetectionStrategy, Component, DestroyRef, ElementRef, inject, input, output, viewChild } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

/** What the last save said: it worked, or the server's reason it did not. */
export interface SaveStatus {
  readonly kind: 'saved' | 'refused';
  readonly text: string;
}

/**
 * The setup's name and Save setup, Check data, and Start backtest with its
 * shortcut. Beside the form it is the top card of the page's run rail. Where
 * the rail is stacked under the form it is a bar kept in view at the bottom of
 * the page instead, fixed to the window rather than sticky, because the
 * shell's `main` scrolls horizontally there and so would hold a sticky bar in
 * place; the bar says its measured height so that the page can keep its end
 * clear of it.
 */
@Component({
  selector: 'app-options-backtest-bottom-bar',
  imports: [FormField, InputText, ButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bottom-bar.component.html',
  styleUrl: './bottom-bar.component.scss',
})
export class BottomBarComponent {
  readonly nameField = input.required<FieldTree<string>>();
  readonly canSave = input(false);
  readonly fixedOnNarrow = input(true);
  readonly saving = input(false);
  /** Why the name cannot be used. */
  readonly nameProblem = input<string | null>(null);
  /** Why Save is not available yet, when it is not. */
  readonly hint = input<string | null>(null);
  readonly status = input<SaveStatus | null>(null);
  /** Whether Check data can be asked: the setup is read back and both dates are picked. */
  readonly canCheck = input(false);
  /** Whether a backtest can be started: the same, and none is going. */
  readonly canStart = input(false);
  /** What Check data and Start wait for, or that a backtest is going. */
  readonly runHint = input<string | null>(null);
  readonly save = output();
  readonly nameEdited = output();
  readonly generateName = output();
  readonly automaticName = input(true);
  readonly check = output();
  readonly start = output();
  /** The bar's height, to keep the end of the page clear of it while it is fixed to the window. */
  readonly height = output<number>();
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      if (typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(() => this.height.emit(Math.ceil(this.panel().nativeElement.getBoundingClientRect().height)));
      observer.observe(this.panel().nativeElement);
      destroyRef.onDestroy(() => observer.disconnect());
    });
  }

  protected submit(): void {
    if (this.canSave()) this.save.emit();
  }

}
