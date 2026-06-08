import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LogLine } from '../../../api/live-runs.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerRunLogModalComponent } from './broker-run-log-modal.component';

const LINES: LogLine[] = [
  { ts_ms: 1_700_000_000_000, raw_text: 'bar 09:45 SPY 624.10', event_type: 'bar', consolidator_emitted: 1, snapshot_set: '{}' },
  { ts_ms: 1_700_000_001_000, raw_text: 'HALT outside_mutation: unexpected SPY +50', event_type: 'raw', consolidator_emitted: null, snapshot_set: null },
];

class FakeLiveRunsService {
  getLogTail = vi.fn().mockResolvedValue(LINES);
}

// Host harness so we can assert on the (closed) output without poking internals.
@Component({
  imports: [BrokerRunLogModalComponent],
  template: `<app-broker-run-log-modal [runId]="runId()" [live]="false" (closed)="closeCount.set(closeCount() + 1)" />`,
})
class HostComponent {
  readonly runId = signal('run-abc123');
  readonly closeCount = signal(0);
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
}

let activeFixture: { destroy(): void } | null = null;

function setup() {
  const svc = new FakeLiveRunsService();
  TestBed.configureTestingModule({
    providers: [{ provide: LiveRunsService, useValue: svc }],
  });
  const fixture = TestBed.createComponent(HostComponent);
  activeFixture = fixture;
  fixture.detectChanges();
  return { fixture, svc, host: fixture.componentInstance };
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerRunLogModalComponent', () => {
  it('fetches and renders the run log tail for the given run', async () => {
    const { fixture, svc } = setup();
    await flush();
    fixture.detectChanges();

    expect(svc.getLogTail).toHaveBeenCalledWith('run-abc123', expect.any(Number));
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('bar 09:45 SPY 624.10');
    expect(text).toContain('HALT outside_mutation');
  });

  it('shows the full run id as the link back to the run', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.runlog-runid')?.textContent).toContain('run-abc123');
  });

  it('emits close when the close button is clicked', async () => {
    const { fixture, host } = setup();
    await flush();
    fixture.detectChanges();

    fixture.nativeElement.querySelector('.runlog-close')?.click();
    expect(host.closeCount()).toBe(1);
  });

  it('emits close when the backdrop is clicked', async () => {
    const { fixture, host } = setup();
    await flush();
    fixture.detectChanges();

    fixture.nativeElement.querySelector('.runlog-backdrop')?.click();
    expect(host.closeCount()).toBe(1);
  });

  it('emits close on Escape', async () => {
    const { fixture, host } = setup();
    await flush();
    fixture.detectChanges();

    fixture.nativeElement
      .querySelector('.runlog-dialog')
      ?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(host.closeCount()).toBe(1);
  });

  it('traps Tab focus inside the dialog (wraps last control back to first)', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const root = fixture.nativeElement;
    const close = root.querySelector('.runlog-close') as HTMLElement | null;
    const copy = root.querySelector('.runlog-copy') as HTMLElement | null;
    close?.focus();
    expect(root.ownerDocument.activeElement).toBe(close);

    close?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    fixture.detectChanges();

    expect(root.ownerDocument.activeElement).toBe(copy);
  });

  it('copies the run id to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    fixture.nativeElement.querySelector('.runlog-copy')?.click();
    await flush();
    fixture.detectChanges();

    expect(writeText).toHaveBeenCalledWith('run-abc123');
    expect(fixture.nativeElement.querySelector('.runlog-copy')?.textContent).toContain('Copied');
  });
});
