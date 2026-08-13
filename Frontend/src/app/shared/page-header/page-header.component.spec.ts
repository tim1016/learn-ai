import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
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

  it('still hides the guide region when nothing is projected', () => {
    const fixture = TestBed.createComponent(NoGuideHost);
    fixture.detectChanges();
    const guide = find(fixture.nativeElement as HTMLElement, '.page-header-guide');
    expect(guide.children.length).toBe(0);
  });

  it('renders title and actions slot without a subtitle', () => {
    const fixture = TestBed.createComponent(NoGuideHost);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(find(el, '.page-title').textContent?.trim()).toBe('Plain Page');
    expect(el.querySelector('.page-subtitle')).toBeNull();
    expect(find(el, '[slot=actions]').textContent).toBe('Save');
  });
});
