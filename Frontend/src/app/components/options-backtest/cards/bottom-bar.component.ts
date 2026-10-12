import { afterNextRender, ChangeDetectionStrategy, Component, DestroyRef, ElementRef, inject, input, output, signal, viewChild } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

/** What the last save said: it worked, or the server's reason it did not. */
export interface SaveStatus {
  readonly kind: 'saved' | 'refused';
  readonly text: string;
}

/**
 * The bar kept in view at the bottom of the page: the setup's name and Save
 * setup. It is fixed to the window rather than sticky, because the shell's
 * `main` scrolls horizontally and so would hold a sticky bar in place; a
 * spacer of the bar's measured height keeps the end of the page clear of it.
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
  readonly saving = input(false);
  /** Why the name cannot be used. */
  readonly nameProblem = input<string | null>(null);
  /** Why Save is not available yet, when it is not. */
  readonly hint = input<string | null>(null);
  readonly status = input<SaveStatus | null>(null);
  readonly save = output();

  /** The fixed bar's height, reserved at the end of the page. */
  protected readonly reserved = signal(0);
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      if (typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(() => this.reserved.set(Math.ceil(this.panel().nativeElement.getBoundingClientRect().height)));
      observer.observe(this.panel().nativeElement);
      destroyRef.onDestroy(() => observer.disconnect());
    });
  }

  protected submit(): void {
    if (this.canSave()) this.save.emit();
  }
}
