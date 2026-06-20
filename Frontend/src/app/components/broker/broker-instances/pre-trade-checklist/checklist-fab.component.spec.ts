import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { ChecklistFabComponent } from './checklist-fab.component';

interface Harness {
  el: HTMLElement;
  toggled: number;
  setFailing(n: number): void;
  setVisible(v: boolean): void;
}

function render(opts: {
  visible?: boolean;
  failingCount?: number;
}): Harness {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ChecklistFabComponent);
  fixture.componentRef.setInput('visible', opts.visible ?? true);
  fixture.componentRef.setInput('failingCount', opts.failingCount ?? 0);
  let toggled = 0;
  fixture.componentInstance.toggleRequested.subscribe(() => (toggled += 1));
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    get toggled() {
      return toggled;
    },
    setFailing(n) {
      fixture.componentRef.setInput('failingCount', n);
      fixture.detectChanges();
    },
    setVisible(v) {
      fixture.componentRef.setInput('visible', v);
      fixture.detectChanges();
    },
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('ChecklistFabComponent', () => {
  it('renders the FAB when visible is true', () => {
    const h = render({ visible: true });
    expect(h.el.querySelector('[data-testid="pre-trade-fab"]')).not.toBeNull();
  });

  it('renders nothing when visible is false', () => {
    const h = render({ visible: false });
    expect(h.el.querySelector('[data-testid="pre-trade-fab"]')).toBeNull();
  });

  it('shows the failing-count badge when failingCount > 0', () => {
    const h = render({ failingCount: 3 });
    const badge = h.el.querySelector('[data-testid="pre-trade-fab-badge"]');
    expect(badge?.textContent?.trim()).toBe('3');
  });

  it('hides the badge when failingCount is 0', () => {
    const h = render({ failingCount: 0 });
    expect(h.el.querySelector('[data-testid="pre-trade-fab-badge"]')).toBeNull();
  });

  it('emits toggleRequested on click', () => {
    const h = render({ failingCount: 1 });
    h.el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    expect(h.toggled).toBe(1);
  });
});
