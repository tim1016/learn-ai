import { afterNextRender, ChangeDetectionStrategy, Component, computed, ElementRef, input, output, signal, viewChild } from '@angular/core';

/**
 * A step-through callout (#2821): one reading step at a time with Back,
 * Next and Done. Arrow keys move, Esc exits. It opens with focus on itself
 * and reports each step so its host can light up that step's target; the
 * host returns focus to whatever opened it when it closes.
 */
@Component({
  selector: 'app-golden-search-walkthrough',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-walkthrough.component.html',
  styleUrl: './golden-search-walkthrough.component.scss',
})
export class GoldenSearchWalkthroughComponent {
  readonly steps = input.required<readonly { readonly text: string }[]>();
  /** Names the walkthrough, e.g. "Walkthrough of Equity and fall from peak". */
  readonly label = input.required<string>();
  readonly stepChange = output<number>();
  readonly closed = output();

  private readonly callout = viewChild.required<ElementRef<HTMLElement>>('callout');
  protected readonly index = signal(0);
  protected readonly step = computed(() => this.steps()[this.index()]);
  protected readonly isFirst = computed(() => this.index() === 0);
  protected readonly isLast = computed(() => this.index() === this.steps().length - 1);

  constructor() {
    afterNextRender(() => {
      this.callout().nativeElement.focus();
      this.stepChange.emit(0);
    });
  }

  protected back(): void {
    if (!this.isFirst()) this.go(this.index() - 1);
  }

  protected forward(): void {
    if (this.isLast()) this.closed.emit();
    else this.go(this.index() + 1);
  }

  protected onKey(event: KeyboardEvent): void {
    if (event.key === 'Escape') this.closed.emit();
    else if (event.key === 'ArrowRight' && !this.isLast()) this.go(this.index() + 1);
    else if (event.key === 'ArrowLeft') this.back();
    else return;
    event.preventDefault();
    event.stopPropagation();
  }

  private go(index: number): void {
    this.index.set(index);
    this.stepChange.emit(index);
  }
}
