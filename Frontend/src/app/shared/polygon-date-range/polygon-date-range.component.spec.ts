import { Component, signal } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { Observable, of, throwError } from 'rxjs';

import { PolygonDateRangeComponent } from './polygon-date-range.component';
import { MarketMonitorService } from '../../services/market-monitor.service';
import type { MarketHolidayEvent } from '../../models/market-monitor';

@Component({
  imports: [PolygonDateRangeComponent],
  template: `
    <app-polygon-date-range
      [(fromDate)]="from"
      [(toDate)]="to"
      idPrefix="test"
    />
  `,
})
class HostComponent {
  from = signal('2025-01-01');
  to = signal('2025-03-31');
}

interface FakeMonitor {
  getHolidays: ReturnType<typeof vi.fn>;
}

function makeMonitor(returnValue: Observable<MarketHolidayEvent[]> = of([])): FakeMonitor {
  return {
    getHolidays: vi.fn().mockReturnValue(returnValue),
  };
}

async function mount(monitor: FakeMonitor): Promise<ComponentFixture<HostComponent>> {
  await TestBed.configureTestingModule({
    imports: [HostComponent, NoopAnimationsModule],
    providers: [{ provide: MarketMonitorService, useValue: monitor }],
  }).compileComponents();

  const fixture = TestBed.createComponent(HostComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

describe('PolygonDateRangeComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
  });

  it('renders both date inputs with stable label-for wiring', async () => {
    const fixture = await mount(makeMonitor());

    const root = fixture.nativeElement as HTMLElement;
    const fromInput = root.querySelector('#test-from');
    const toInput = root.querySelector('#test-to');

    expect(fromInput).not.toBeNull();
    expect(toInput).not.toBeNull();
  });

  it('fetches holidays from MarketMonitorService once on construct', async () => {
    const monitor = makeMonitor();
    await mount(monitor);

    expect(monitor.getHolidays).toHaveBeenCalledTimes(1);
    expect(monitor.getHolidays).toHaveBeenCalledWith(20);
  });

  it('disables holiday dates returned by the service', async () => {
    const christmas: MarketHolidayEvent = {
      date: '2025-12-25',
      exchanges: ['NYSE'],
      name: 'Christmas',
      status: 'Closed',
      open: null,
      close: null,
    };
    const fixture = await mount(makeMonitor(of([christmas])));

    const pdr = fixture.debugElement.children[0].children[0]
      .componentInstance as PolygonDateRangeComponent;
    const disabled = (
      pdr as unknown as { disabledDates: () => Date[] }
    ).disabledDates();

    expect(disabled.length).toBe(1);
    expect(disabled[0].getFullYear()).toBe(2025);
    expect(disabled[0].getMonth()).toBe(11); // December
    expect(disabled[0].getDate()).toBe(25);
  });

  it('renders without throwing when getHolidays rejects', async () => {
    const fixture = await mount(makeMonitor(throwError(() => new Error('network down'))));

    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('#test-from')).not.toBeNull();
  });
});
