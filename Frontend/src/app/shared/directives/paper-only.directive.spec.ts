import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import { BrokerHealthService } from '../../services/broker-health.service';
import { PaperOnlyDirective } from './paper-only.directive';

class FakeBrokerHealthService {
  readonly isPaperConnected = signal(false);
}

@Component({
  imports: [PaperOnlyDirective],
  template: `<button appPaperOnly>Submit</button>`,
})
class HostComponent {}

@Component({
  imports: [PaperOnlyDirective],
  template: `<button appPaperOnly appPaperOnlyTooltip="Custom tooltip">Submit</button>`,
})
class CustomTooltipHost {}

describe('PaperOnlyDirective', () => {
  function setup<T>(component: new () => T, paperConnected: boolean) {
    const fake = new FakeBrokerHealthService();
    fake.isPaperConnected.set(paperConnected);
    TestBed.configureTestingModule({
      providers: [{ provide: BrokerHealthService, useValue: fake }],
    });
    const fixture = TestBed.createComponent(component);
    fixture.detectChanges();
    return { fake, fixture };
  }

  function findButton(fixture: { nativeElement: unknown }): HTMLButtonElement {
    const btn = (fixture.nativeElement as HTMLElement).querySelector('button');
    if (btn === null) throw new Error('expected a <button> in the host template');
    return btn as HTMLButtonElement;
  }

  it('disables the host when broker is not paper-connected', () => {
    const { fixture } = setup(HostComponent, false);
    const btn = findButton(fixture);
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute('aria-disabled')).toBe('true');
    expect(btn.getAttribute('title')).toContain('paper account');
  });

  it('enables the host when broker is paper-connected', () => {
    const { fixture } = setup(HostComponent, true);
    const btn = findButton(fixture);
    expect(btn.disabled).toBe(false);
    expect(btn.getAttribute('aria-disabled')).toBe('false');
    expect(btn.getAttribute('title')).toBeNull();
  });

  it('uses the custom tooltip when supplied', () => {
    const { fixture } = setup(CustomTooltipHost, false);
    const btn = findButton(fixture);
    expect(btn.getAttribute('title')).toBe('Custom tooltip');
  });

  it('reacts to broker connection state changes', () => {
    const { fake, fixture } = setup(HostComponent, false);
    const btn = findButton(fixture);
    expect(btn.disabled).toBe(true);

    fake.isPaperConnected.set(true);
    fixture.detectChanges();
    expect(btn.disabled).toBe(false);
  });
});
