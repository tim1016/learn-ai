import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { ConfigSectionComponent } from './config-section.component';

@Component({
  imports: [ConfigSectionComponent],
  template: `
    <app-config-section
      title="Time window"
      index="01"
      [configured]="configured()"
      [summary]="summary()"
      [(open)]="open"
    >
      <input data-testid="body-input" />
    </app-config-section>
  `,
})
class Host {
  readonly configured = signal(true);
  readonly summary = signal('SPY · 2024-01 → 06 · minute');
  open = true;
}

function el(fixture: { nativeElement: HTMLElement }, selector: string): Element | null {
  return fixture.nativeElement.querySelector(selector);
}

describe('ConfigSectionComponent', () => {
  it('shows the projected body while open and hides the summary', () => {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();

    expect(el(fixture, '[data-testid="body-input"]')).not.toBeNull();
    expect(el(fixture, '.config-section__summary')).toBeNull();
    expect(el(fixture, '.config-section__header')?.getAttribute('aria-expanded')).toBe('true');
  });

  it('folds to the one-line summary when collapsed and configured', () => {
    const fixture = TestBed.createComponent(Host);
    fixture.componentInstance.open = false;
    fixture.detectChanges();

    expect(el(fixture, '[data-testid="body-input"]')).toBeNull();
    expect(el(fixture, '.config-section__summary')?.textContent?.trim()).toBe(
      'SPY · 2024-01 → 06 · minute',
    );
  });

  it('toggles open state from the header and writes it back to the parent', () => {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();

    (el(fixture, '.config-section__header') as HTMLButtonElement).click();
    fixture.detectChanges();

    expect(fixture.componentInstance.open).toBe(false);
    expect(el(fixture, '[data-testid="body-input"]')).toBeNull();
  });
});
