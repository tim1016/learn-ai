import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MarkdownViewerComponent } from './markdown-viewer.component';

const SRC = '/assets/docs/fixture.md';

function renderMarkdown(markdown: string): HTMLElement {
  TestBed.configureTestingModule({
    imports: [MarkdownViewerComponent],
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  const fixture = TestBed.createComponent(MarkdownViewerComponent);
  fixture.componentRef.setInput('src', SRC);
  fixture.detectChanges();
  TestBed.inject(HttpTestingController).expectOne(SRC).flush(markdown);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

function find(root: HTMLElement, selector: string): Element {
  const element = root.querySelector(selector);
  if (element === null) throw new Error(`rendered markdown has no ${selector}`);
  return element;
}

function click(target: Element): MouseEvent {
  const event = new MouseEvent('click', { bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

describe('MarkdownViewerComponent', () => {
  let scrollIntoView: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    scrollIntoView = vi.spyOn(Element.prototype, 'scrollIntoView');
  });

  afterEach(() => {
    scrollIntoView.mockRestore();
  });

  it('scrolls to a same-page heading instead of leaving the page', () => {
    const root = renderMarkdown('[Jump](#target-heading)\n\n## Target heading\n');

    const event = click(find(root, 'a[href="#target-heading"]'));

    expect(event.defaultPrevented).toBe(true);
    expect(scrollIntoView.mock.contexts[0]).toBe(root.querySelector('h2#target-heading'));
  });

  it('leaves links to other pages to the browser', () => {
    const root = renderMarkdown('[Elsewhere](https://example.com/page)\n');

    const event = click(find(root, 'a'));

    expect(event.defaultPrevented).toBe(false);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('keeps inline SVG diagrams, and their links jump within the page', () => {
    const root = renderMarkdown(
      [
        '<figure class="md-diagram">',
        '<svg viewBox="0 0 10 10" role="img" aria-label="Diagram">',
        '<a href="#the-lanes"><rect class="dg-box" width="5" height="5" /></a>',
        '</svg>',
        '</figure>',
        '',
        '## The lanes',
        '',
      ].join('\n'),
    );

    const event = click(find(root, 'svg rect.dg-box'));

    expect(event.defaultPrevented).toBe(true);
    expect(scrollIntoView.mock.contexts[0]).toBe(root.querySelector('h2#the-lanes'));
  });

  it('still removes scripts and event handlers', () => {
    const root = renderMarkdown(
      '<svg><script>window.pwned = true</script></svg>\n\n<img src="x.png" onerror="window.pwned = true">\n',
    );

    expect(root.querySelector('script')).toBeNull();
    expect(root.querySelector('img')?.hasAttribute('onerror')).toBe(false);
  });
});
