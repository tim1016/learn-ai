import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';
import type { SessionDataCapability } from '../../../api/broker-models';
import { BrokerCapabilityPanelComponent } from './broker-capability-panel.component';

const SNAPSHOT: SessionDataCapability = {
  symbol: 'SPY',
  con_id: 756733,
  account_mode: 'live',
  account_id: 'U1234567',
  probed_at_ms: Date.UTC(2026, 6, 2, 16, 0, 0),
  time_zone_id: 'America/New_York',
  raw_evidence: [],
  sessions: {
    RTH: {
      window_today_open_ms: Date.UTC(2026, 6, 2, 13, 30, 0),
      window_today_close_ms: Date.UTC(2026, 6, 2, 20, 0, 0),
      data: 'live',
      tradeable: 'yes',
      order_eligible_outside_rth: true,
      evidence_codes: [],
    },
    PRE: {
      window_today_open_ms: Date.UTC(2026, 6, 2, 8, 0, 0),
      window_today_close_ms: Date.UTC(2026, 6, 2, 13, 30, 0),
      data: 'live',
      tradeable: 'yes',
      order_eligible_outside_rth: true,
      evidence_codes: [],
    },
    POST: {
      window_today_open_ms: Date.UTC(2026, 6, 2, 20, 0, 0),
      window_today_close_ms: Date.UTC(2026, 6, 3, 0, 0, 0),
      data: 'delayed',
      tradeable: 'needs_enablement',
      order_eligible_outside_rth: false,
      evidence_codes: [354],
    },
    OVERNIGHT: {
      window_today_open_ms: null,
      window_today_close_ms: null,
      data: 'none',
      tradeable: 'no',
      order_eligible_outside_rth: false,
      evidence_codes: [],
    },
  },
};

@Component({
  imports: [BrokerCapabilityPanelComponent],
  template: `
    <app-broker-capability-panel
      [snapshots]="snapshots"
      [connected]="connected"
      (probe)="probe()"
    />
  `,
})
class CapabilityHost {
  snapshots = [SNAPSHOT];
  connected = true;
  probe = vi.fn();
}

describe('BrokerCapabilityPanelComponent', () => {
  it('renders the per-session capability matrix in plain words', () => {
    const fixture = TestBed.createComponent(CapabilityHost);
    fixture.detectChanges();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';

    expect(text).toContain('SPY');
    expect(text).toContain('RTH');
    expect(text).toContain('live + tradeable');
    expect(text).toContain('delayed + enablement needed');
    expect(text).toContain('no data + not enabled');
    expect(text).toContain('Codes 354');
    expect(text).toContain('2026-07-02 12:00:00 ET');
  });

  it('emits probe requests from the action button', () => {
    const fixture = TestBed.createComponent(CapabilityHost);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector('button');
    button?.dispatchEvent(new MouseEvent('click', { bubbles: true }));

    expect(fixture.componentInstance.probe).toHaveBeenCalledOnce();
  });
});
