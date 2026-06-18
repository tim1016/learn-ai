import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { DetectiveSectionComponent } from './detective-section.component';
import type { DetectiveTab } from './detective-tab';

function render(opts: { activeTab?: DetectiveTab } = {}): {
  el: HTMLElement;
  component: DetectiveSectionComponent;
} {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(DetectiveSectionComponent);
  fixture.componentRef.setInput('activeTab', opts.activeTab ?? 'activity');
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('DetectiveSectionComponent', () => {
  it('marks the Activity tab as selected by default', () => {
    const { el } = render();

    const activityTab = el.querySelector<HTMLElement>('[data-testid="detective-tab-activity"]');
    expect(activityTab?.getAttribute('aria-selected')).toBe('true');
  });

  it('marks the Diagnostics tab as selected when activeTab is "diagnostics"', () => {
    const { el } = render({ activeTab: 'diagnostics' });

    const diagnosticsTab = el.querySelector<HTMLElement>(
      '[data-testid="detective-tab-diagnostics"]',
    );
    expect(diagnosticsTab?.getAttribute('aria-selected')).toBe('true');
  });

  it('emits tabRequested with the clicked tab id', () => {
    const { el, component } = render({ activeTab: 'activity' });
    let lastEmitted: DetectiveTab | null = null;
    component.tabRequested.subscribe((tab) => (lastEmitted = tab));

    el.querySelector<HTMLButtonElement>('[data-testid="detective-tab-diagnostics"]')?.click();

    expect(lastEmitted).toBe('diagnostics');
  });
});
