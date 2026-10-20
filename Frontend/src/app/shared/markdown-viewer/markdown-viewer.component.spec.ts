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

  it.each([
    ['a link to a same-page heading', '[Jump](#target-heading)\n\n## Target heading\n', 'a[href="#target-heading"]', 'h2#target-heading'],
    [
      'a link inside an inline SVG diagram, which the page keeps',
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
      'svg rect.dg-box',
      'h2#the-lanes',
    ],
  ])('scrolls to the heading instead of leaving the page for %s', (_name, markdown, clicked, heading) => {
    const root = renderMarkdown(markdown);

    const event = click(find(root, clicked));

    expect(event.defaultPrevented).toBe(true);
    expect(scrollIntoView.mock.contexts[0]).toBe(root.querySelector(heading));
  });

  it('leaves links to other pages to the browser', () => {
    const root = renderMarkdown('[Elsewhere](https://example.com/page)\n');

    const event = click(find(root, 'a'));

    expect(event.defaultPrevented).toBe(false);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('still removes scripts, event handlers, page-wide CSS and form controls', () => {
    const root = renderMarkdown(
      [
        '<svg><script>window.pwned = true</script><style>body { display: none }</style></svg>',
        '',
        '<img src="x.png" onerror="window.pwned = true">',
        '',
        '<style>body { display: none }</style>',
        '',
        '<form action="https://example.com"><input type="password"><button>Go</button></form>',
        '',
      ].join('\n'),
    );

    expect(root.querySelector('script')).toBeNull();
    expect(root.querySelector('img')?.hasAttribute('onerror')).toBe(false);
    expect(root.querySelector('style, form, input, button')).toBeNull();
  });

  it('refuses a document from outside the served docs folder', () => {
    TestBed.configureTestingModule({
      imports: [MarkdownViewerComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(MarkdownViewerComponent);
    fixture.componentRef.setInput('src', 'https://example.com/doc.md');
    fixture.detectChanges();

    TestBed.inject(HttpTestingController).verify();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'only documents under /assets/docs/ are rendered',
    );
  });
});
