import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';
import { PageHeaderComponent } from './page-header.component';

@Component({
  imports: [PageHeaderComponent],
  template: `
    <app-page-header title="Strategy Builder">
      <button slot="actions">Save</button>
      <p slot="guide" data-testid="projected-guide">How this page works</p>
    </app-page-header>
  `,
})
class GuideHost {}

@Component({
  imports: [PageHeaderComponent],
  template: `
    <app-page-header title="Plain Page">
      <button slot="actions">Save</button>
    </app-page-header>
  `,
})
class NoGuideHost {}

@Component({
  imports: [PageHeaderComponent],
  template: `<app-page-header [title]="title" />`,
})
class TitledHost {
  title = 'Golden Search';
}

@Component({ template: '' })
class RouteStub {}

function find(el: HTMLElement, selector: string): Element {
  const node = el.querySelector(selector);
  if (node === null) throw new Error(`expected element matching ${selector}`);
  return node;
}

describe('PageHeaderComponent slots', () => {
  it('projects content into the guide slot when provided', () => {
    const fixture = TestBed.createComponent(GuideHost);
    fixture.detectChanges();
    const projected = find(
      fixture.nativeElement as HTMLElement,
      '[data-testid="projected-guide"]',
    );
    expect(projected.textContent).toBe('How this page works');
  });

  it('renders a header with no guide projected: guide region empty, title and actions slot shown, no subtitle', () => {
    const fixture = TestBed.createComponent(NoGuideHost);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const guide = find(el, '.page-header-guide');
    expect.soft(guide.children.length, 'still hides the guide region when nothing is projected').toBe(0);
    expect.soft(find(el, '.page-title').textContent?.trim(), 'renders the title').toBe('Plain Page');
    expect.soft(el.querySelector('.page-subtitle'), 'renders no subtitle').toBeNull();
    expect.soft(find(el, '[slot=actions]').textContent, 'renders the actions slot').toBe('Save');
  });
});

describe('PageHeaderComponent trail', () => {
  const routes = [
    { path: 'golden-search/:studyId', component: RouteStub },
    { path: 'docs/broker-manual', component: RouteStub },
    { path: '**', component: RouteStub, data: { notFound: true } },
  ];

  async function trailAt(url: string, title: string): Promise<string[]> {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    await TestBed.inject(Router).navigateByUrl(url);
    const fixture = TestBed.createComponent(TitledHost);
    fixture.componentInstance.title = title;
    fixture.detectChanges();
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.page-breadcrumb a')).map(
      (link) => `${link.textContent?.trim()} -> ${link.getAttribute('href')}`,
    );
  }

  it('leads a page back to its menu group, landing on the group\'s first page', async () => {
    expect(await trailAt('/golden-search/study-9', 'Golden Search')).toEqual(['Signal strategies -> /signal-strategies']);
  });

  it('also leads back to the listed page a deeper heading sits inside', async () => {
    expect(await trailAt('/golden-search/study-9', 'Study 9')).toEqual([
      'Signal strategies -> /signal-strategies',
      'Golden Search -> /golden-search',
    ]);
  });

  it.each([
    ['a document, which belongs to no group', '/docs/broker-manual'],
    ['an address no page answers, which must not borrow a prefix\'s group', '/golden-search/study-9/nothing'],
  ])('draws no trail for %s', async (_name, url) => {
    expect(await trailAt(url, 'Anything')).toEqual([]);
  });
});
