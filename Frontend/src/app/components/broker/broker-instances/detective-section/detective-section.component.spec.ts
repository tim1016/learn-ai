import { Component, provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { DetectiveSectionComponent, type DetectiveTab } from './detective-section.component';

@Component({
  selector: 'app-test-host',
  imports: [DetectiveSectionComponent],
  template: `
    <app-detective-section
      [tabbed]="tabbed"
      [activeTab]="activeTab"
      (tabRequested)="onTabRequested($event)"
    >
      <div slot="activity" data-testid="activity-body">ACTIVITY BODY</div>
      <div slot="diagnostics" data-testid="diagnostics-body">DIAGNOSTICS BODY</div>
    </app-detective-section>
  `,
})
class TestHostComponent {
  tabbed = false;
  activeTab: DetectiveTab = 'activity';
  lastEmitted: DetectiveTab | null = null;
  onTabRequested(tab: DetectiveTab): void {
    this.lastEmitted = tab;
  }
}

function render(opts: { tabbed?: boolean; activeTab?: DetectiveTab } = {}): {
  el: HTMLElement;
  host: TestHostComponent;
} {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(TestHostComponent);
  fixture.componentInstance.tabbed = opts.tabbed ?? false;
  fixture.componentInstance.activeTab = opts.activeTab ?? 'activity';
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    host: fixture.componentInstance,
  };
}

function isHidden(el: Element | null): boolean {
  return !!el?.classList.contains('hidden');
}

afterEach(() => TestBed.resetTestingModule());

describe('DetectiveSectionComponent', () => {
  describe('untabbed (legacy) mode', () => {
    it('does not render the tab strip', () => {
      const { el } = render({ tabbed: false });

      expect(el.querySelector('[data-testid="detective-tab-activity"]')).toBeNull();
      expect(el.querySelector('[data-testid="detective-tab-diagnostics"]')).toBeNull();
    });

    it('renders both projected slots inline', () => {
      const { el } = render({ tabbed: false });

      expect(isHidden(el.querySelector('.slot-activity'))).toBe(false);
      expect(isHidden(el.querySelector('.slot-diagnostics'))).toBe(false);
      expect(el.textContent ?? '').toContain('ACTIVITY BODY');
      expect(el.textContent ?? '').toContain('DIAGNOSTICS BODY');
    });
  });

  describe('tabbed (cockpit) mode', () => {
    it('renders the tab strip', () => {
      const { el } = render({ tabbed: true });

      expect(el.querySelector('[data-testid="detective-tab-activity"]')).not.toBeNull();
      expect(el.querySelector('[data-testid="detective-tab-diagnostics"]')).not.toBeNull();
    });

    it('marks the Activity tab as selected by default', () => {
      const { el } = render({ tabbed: true });

      const activityTab = el.querySelector<HTMLElement>('[data-testid="detective-tab-activity"]');
      expect(activityTab?.getAttribute('aria-selected')).toBe('true');
    });

    it('hides the diagnostics slot when activity is active', () => {
      const { el } = render({ tabbed: true, activeTab: 'activity' });

      expect(isHidden(el.querySelector('.slot-activity'))).toBe(false);
      expect(isHidden(el.querySelector('.slot-diagnostics'))).toBe(true);
    });

    it('hides the activity slot when diagnostics is active', () => {
      const { el } = render({ tabbed: true, activeTab: 'diagnostics' });

      expect(isHidden(el.querySelector('.slot-activity'))).toBe(true);
      expect(isHidden(el.querySelector('.slot-diagnostics'))).toBe(false);
    });

    it('emits tabRequested with the clicked tab id', () => {
      const { el, host } = render({ tabbed: true, activeTab: 'activity' });

      el.querySelector<HTMLButtonElement>('[data-testid="detective-tab-diagnostics"]')?.click();

      expect(host.lastEmitted).toBe('diagnostics');
    });
  });
});
