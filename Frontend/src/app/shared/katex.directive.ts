import {
  Directive,
  ElementRef,
  input,
  effect,
  inject,
} from '@angular/core';
import katex from 'katex';

@Directive({
  selector: '[appKatex]',
  standalone: true,
})
export class KatexDirective {
  appKatex = input.required<string>();
  displayMode = input(false);

  private el = inject(ElementRef);

  constructor() {
    effect(() => {
      const latex = this.appKatex();
      if (!latex) return;

      try {
        katex.render(latex, this.el.nativeElement, {
          throwOnError: false,
          displayMode: this.displayMode(),
        });
      } catch {
        this.el.nativeElement.textContent = latex;
      }
    });
  }
}
