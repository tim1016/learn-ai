/* eslint-disable @typescript-eslint/no-non-null-assertion */
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { RunDockComponent } from './run-dock.component';
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
  RunDockSource,
  RunLogEntry,
} from './run-dock-source';

/** Hand-rolled fake source. Each signal is mutable from the test so we
 *  can drive the dock into idle / active / done / error states without
 *  pulling in real services. */
class FakeRunDockSource implements RunDockSource {
  readonly dockState = signal<'idle' | 'active' | 'done' | 'error'>('idle');
  readonly headline = signal<string>('idle — no run');
  readonly headlineLevel = signal<'info' | 'success' | 'warn' | 'error'>('info');
  readonly progressPercent = signal<number | null>(null);
  readonly etaText = signal<string | null>(null);
  readonly canCancel = signal<boolean>(false);
  readonly log = signal<readonly RunLogEntry[]>([]);
  clearLogCount = 0;
  cancelCount = 0;
  clearLog(): void {
    this.clearLogCount += 1;
  }
  cancel(): void {
    this.cancelCount += 1;
  }
}

const STORAGE_KEY = 'run-dock-expanded:test';

@Component({
  selector: 'app-host',
  imports: [RunDockComponent],
  template: `<app-run-dock />`,
})
class HostComponent {}

function configure(initialStorage: string | null): {
  fixture: ReturnType<typeof TestBed.createComponent<HostComponent>>;
  source: FakeRunDockSource;
} {
  if (initialStorage === null) {
    localStorage.removeItem(STORAGE_KEY);
  } else {
    localStorage.setItem(STORAGE_KEY, initialStorage);
  }
  const source = new FakeRunDockSource();
  TestBed.configureTestingModule({
    providers: [
      { provide: RUN_DOCK_SOURCE, useValue: source },
      { provide: RUN_DOCK_STORAGE_KEY, useValue: STORAGE_KEY },
    ],
  });
  const fixture = TestBed.createComponent(HostComponent);
  fixture.detectChanges();
  return { fixture, source };
}

describe('RunDockComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  it('defaults to collapsed on first mount when localStorage is empty', () => {
    const { fixture } = configure(null);
    const host = fixture.nativeElement as HTMLElement;
    // Collapsed mode renders the strip button (no <header>).
    expect(host.querySelector('.run-dock__strip')).not.toBeNull();
    expect(host.querySelector('.run-dock__header')).toBeNull();
  });

  it('honours a persisted "true" in localStorage by mounting expanded', () => {
    const { fixture } = configure('true');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__header')).not.toBeNull();
    expect(host.querySelector('.run-dock__strip')).toBeNull();
  });

  it('honours a persisted "false" in localStorage by mounting collapsed', () => {
    const { fixture } = configure('false');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__strip')).not.toBeNull();
    expect(host.querySelector('.run-dock__header')).toBeNull();
  });

  it('persists the expand action to localStorage and re-renders expanded', () => {
    const { fixture } = configure(null);
    const host = fixture.nativeElement as HTMLElement;
    const strip = host.querySelector<HTMLButtonElement>('.run-dock__strip');
    expect(strip).not.toBeNull();
    strip!.click();
    fixture.detectChanges();
    expect(localStorage.getItem(STORAGE_KEY)).toBe('true');
    expect(host.querySelector('.run-dock__header')).not.toBeNull();
  });

  it('persists the collapse action to localStorage and re-renders collapsed', () => {
    const { fixture } = configure('true');
    const host = fixture.nativeElement as HTMLElement;
    const collapseBtn = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Collapse run dock"]',
    );
    expect(collapseBtn).not.toBeNull();
    collapseBtn!.click();
    fixture.detectChanges();
    expect(localStorage.getItem(STORAGE_KEY)).toBe('false');
    expect(host.querySelector('.run-dock__strip')).not.toBeNull();
  });

  it('reflects the source headline in both collapsed and expanded modes', () => {
    const { fixture, source } = configure(null);
    source.dockState.set('active');
    source.headline.set('engine_backtest · running_indicators');
    source.headlineLevel.set('info');
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__strip-text')?.textContent).toContain(
      'engine_backtest · running_indicators',
    );
    // Expand and confirm the headline is mirrored.
    host.querySelector<HTMLButtonElement>('.run-dock__strip')!.click();
    fixture.detectChanges();
    expect(host.querySelector('.run-dock__headline-text')?.textContent).toContain(
      'engine_backtest · running_indicators',
    );
  });

  it('renders the progress bar only when state is active and progress is determinate', () => {
    const { fixture, source } = configure('true');
    source.dockState.set('active');
    source.progressPercent.set(null);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__progress')).toBeNull();
    source.progressPercent.set(42);
    fixture.detectChanges();
    const bar = host.querySelector('.run-dock__progress');
    expect(bar).not.toBeNull();
    const fill = bar!.querySelector<HTMLElement>('.run-dock__progress-fill');
    expect(fill?.style.width).toBe('42%');
  });

  it('shows the Cancel button only when canCancel is true', () => {
    const { fixture, source } = configure('true');
    source.canCancel.set(false);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__btn--danger')).toBeNull();
    source.canCancel.set(true);
    fixture.detectChanges();
    expect(host.querySelector('.run-dock__btn--danger')).not.toBeNull();
  });

  it('invokes source.cancel() when the user clicks Cancel', () => {
    const { fixture, source } = configure('true');
    source.canCancel.set(true);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    host.querySelector<HTMLButtonElement>('.run-dock__btn--danger')!.click();
    expect(source.cancelCount).toBe(1);
  });

  it('invokes source.clearLog() when the user clicks Clear log', () => {
    const { fixture, source } = configure('true');
    source.log.set([
      {
        id: '1',
        timestamp: Date.now(),
        level: 'info',
        glyph: '·',
        message: 'hello',
      },
    ]);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    const buttons = Array.from(host.querySelectorAll<HTMLButtonElement>('.run-dock__btn'));
    const clearBtn = buttons.find((b) => b.textContent?.includes('Clear log'));
    expect(clearBtn).toBeDefined();
    clearBtn!.click();
    expect(source.clearLogCount).toBe(1);
  });

  it('renders log entries in order when the source provides them', () => {
    const { fixture, source } = configure('true');
    source.log.set([
      { id: '1', timestamp: 1000, level: 'info', glyph: '▸', message: 'staging_data' },
      { id: '2', timestamp: 2000, level: 'info', glyph: '⚙', message: 'launching_sidecar' },
      { id: '3', timestamp: 3000, level: 'success', glyph: '✓', message: 'done' },
    ]);
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    const lines = Array.from(host.querySelectorAll('.run-dock__line'));
    expect(lines).toHaveLength(3);
    expect(lines[0].textContent).toContain('staging_data');
    expect(lines[1].textContent).toContain('launching_sidecar');
    expect(lines[2].textContent).toContain('done');
  });

  it('shows the empty-log placeholder when the log is empty', () => {
    const { fixture } = configure('true');
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.run-dock__log-empty')).not.toBeNull();
  });
});
