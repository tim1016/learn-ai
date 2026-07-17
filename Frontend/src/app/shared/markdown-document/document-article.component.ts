import { ChangeDetectionStrategy, Component, ElementRef, effect, input, signal, viewChild } from '@angular/core';

import type { MarkdownDocument } from './markdown-document.model';
import { DocumentSectionComponent } from './document-section.component';
import { DocumentTocComponent } from './document-toc.component';

@Component({
  selector: 'app-document-article',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DocumentSectionComponent, DocumentTocComponent],
  templateUrl: './document-article.component.html',
  styleUrl: './document-article.component.scss',
})
export class DocumentArticleComponent {
  readonly document = input.required<MarkdownDocument>();
  readonly route = input.required<string>();
  readonly fragment = input<string | null>(null);

  private readonly article = viewChild<ElementRef<HTMLElement>>('article');
  readonly activeSection = signal<string | null>(null);

  constructor() {
    effect(() => {
      const document = this.document();
      const root = this.article()?.nativeElement;
      if (!root) return;

      this.activeSection.set(document.sections[0]?.id ?? null);
      const fragment = this.fragment();
      if (fragment) queueMicrotask(() => this.scrollTo(fragment));
    });

    effect(onCleanup => {
      const root = this.article()?.nativeElement;
      if (!root || typeof IntersectionObserver === 'undefined') return;

      const observer = new IntersectionObserver(entries => {
        const current = entries
          .filter(entry => entry.isIntersecting)
          .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
        if (current) this.activeSection.set(current.target.id);
      }, { rootMargin: '-15% 0px -70% 0px', threshold: [0, 0.25, 0.75] });

      root.querySelectorAll<HTMLElement>('[data-document-section]').forEach(section => observer.observe(section));
      onCleanup(() => observer.disconnect());
    });
  }

  private scrollTo(fragment: string): void {
    const root = this.article()?.nativeElement;
    const target = root?.querySelector<HTMLElement>('#' + CSS.escape(fragment));
    if (!target) return;

    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    this.activeSection.set(target.closest<HTMLElement>('[data-document-section]')?.id ?? fragment);
  }
}
