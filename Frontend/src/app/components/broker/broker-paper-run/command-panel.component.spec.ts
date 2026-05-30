import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CommandPanelComponent } from './command-panel.component';
import type {
  CommandEntry,
  CommandsSummary,
  CommandVerb,
} from '../../../api/live-runs.types';

const NOW = 1_700_000_100_000;
const POLL_MS = 1_000;

function makeEntry(overrides: Partial<CommandEntry> = {}): CommandEntry {
  return {
    seq: 1,
    verb: 'PAUSE',
    status: 'queued',
    reason: null,
    issued_by: 'operator',
    queued_at_ms: NOW - 500,
    acked_at_ms: null,
    outcome: null,
    outcome_detail: null,
    ...overrides,
  };
}

function makeCommands(entries: CommandEntry[]): CommandsSummary {
  return { entries, poll_interval_ms: POLL_MS };
}

@Component({
  imports: [CommandPanelComponent],
  template: `
    <app-command-panel
      [commands]="commands()"
      [nowMs]="now()"
      [busyVerb]="busyVerb()"
      [writeError]="writeError()"
      (issue)="onIssue($event)"
    />
  `,
})
class HostComponent {
  readonly commands = signal<CommandsSummary>(makeCommands([]));
  readonly now = signal(NOW);
  readonly busyVerb = signal<CommandVerb | null>(null);
  readonly writeError = signal<string | null>(null);
  readonly issued: CommandVerb[] = [];
  onIssue(v: CommandVerb): void {
    this.issued.push(v);
  }
}

function setup(commands: CommandsSummary = makeCommands([])) {
  TestBed.configureTestingModule({ imports: [HostComponent] });
  const fixture = TestBed.createComponent(HostComponent);
  fixture.componentInstance.commands.set(commands);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  return { fixture, host: fixture.componentInstance, el };
}

function text(el: HTMLElement): string {
  return (el.textContent ?? '').replace(/\s+/g, ' ');
}

function buttonByText(el: HTMLElement, label: string): HTMLButtonElement | undefined {
  return Array.from(el.querySelectorAll('button')).find(
    (b) => (b.textContent ?? '').trim() === label,
  );
}

function requireButton(el: HTMLElement, label: string): HTMLButtonElement {
  const btn = buttonByText(el, label);
  if (!btn) throw new Error(`Expected a button labelled "${label}"`);
  return btn;
}

function requireEl<T extends Element>(root: ParentNode, selector: string): T {
  const found = root.querySelector<T>(selector);
  if (!found) throw new Error(`Expected an element matching "${selector}"`);
  return found;
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('CommandPanelComponent — controls (UI-4)', () => {
  it('renders all six command verbs', () => {
    const { el } = setup();
    for (const label of ['Pause', 'Resume', 'Stop', 'Flatten', 'Mark Poisoned', 'Reconcile']) {
      expect(buttonByText(el, label)).toBeDefined();
    }
  });

  it('emits the verb when a command button is clicked', () => {
    const { el, host } = setup();
    requireButton(el, 'Flatten').click();
    expect(host.issued).toEqual(['FLATTEN']);
  });

  it('disables every button while a command is in flight', () => {
    const { fixture, el, host } = setup();
    host.busyVerb.set('STOP');
    fixture.detectChanges();
    const sendingBtn = Array.from(el.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('Sending'),
    );
    expect(sendingBtn).toBeDefined();
    expect(requireButton(el, 'Pause').disabled).toBe(true);
  });

  it('shows the parent write error', () => {
    const { fixture, el, host } = setup();
    host.writeError.set('network down');
    fixture.detectChanges();
    expect(text(el)).toContain('network down');
  });
});

describe('CommandPanelComponent — pending/ack timeline (UI-4)', () => {
  it('renders a queued command as queued', () => {
    const { el } = setup(makeCommands([makeEntry({ status: 'queued' })]));
    const t = text(el);
    expect(t).toContain('PAUSE');
    expect(t).toContain('queued');
  });

  it('renders an acknowledged command with its ack time and outcome', () => {
    const { el } = setup(
      makeCommands([
        makeEntry({
          seq: 2,
          verb: 'FLATTEN',
          status: 'acknowledged',
          acked_at_ms: NOW - 100,
          outcome: 'flattened 100 SPY',
        }),
      ]),
    );
    const t = text(el);
    expect(t).toContain('acknowledged');
    expect(t).toContain('flattened 100 SPY');
    expect(t).toContain('acked');
  });

  it('flags a queued command older than three poll intervals as stale', () => {
    const stale = makeEntry({ status: 'queued', queued_at_ms: NOW - POLL_MS * 4 });
    const { el } = setup(makeCommands([stale]));
    const t = text(el);
    expect(t).toContain('stale');
    expect(t).toContain('STALE');
    expect(t).toContain('unacked');
  });

  it('does NOT flag an acknowledged command as stale even if old', () => {
    const acked = makeEntry({
      status: 'acknowledged',
      queued_at_ms: NOW - POLL_MS * 10,
      acked_at_ms: NOW - POLL_MS * 9,
    });
    const { el } = setup(makeCommands([acked]));
    expect(text(el)).not.toContain('STALE');
  });

  it('orders the timeline newest-first by seq', () => {
    const { el } = setup(
      makeCommands([
        makeEntry({ seq: 1, verb: 'PAUSE' }),
        makeEntry({ seq: 3, verb: 'STOP' }),
        makeEntry({ seq: 2, verb: 'RESUME' }),
      ]),
    );
    const seqs = Array.from(el.querySelectorAll('.seq')).map((n) =>
      (n.textContent ?? '').trim(),
    );
    expect(seqs).toEqual(['#3', '#2', '#1']);
  });
});

describe('CommandPanelComponent — accessibility', () => {
  it('exposes the card via an aria-labelledby region with a real heading', () => {
    const { el } = setup();
    const section = requireEl(el, 'section[aria-labelledby]');
    const labelId = section.getAttribute('aria-labelledby') ?? '';
    const heading = el.querySelector(`#${labelId}`);
    expect(heading?.textContent).toContain('Command Channel');
  });

  it('groups the command buttons with an accessible group label', () => {
    const { el } = setup();
    const group = el.querySelector('.command-buttons');
    expect(group?.getAttribute('role')).toBe('group');
    expect(group?.getAttribute('aria-label')).toBeTruthy();
  });

  it('renders the timeline as a labelled list of list items', () => {
    const { el } = setup(
      makeCommands([makeEntry({ seq: 1 }), makeEntry({ seq: 2, verb: 'STOP' })]),
    );
    const list = el.querySelector('ul[aria-label]');
    expect(list?.getAttribute('aria-label')).toBeTruthy();
    expect(list?.querySelectorAll('li').length).toBe(2);
  });

  it('every command button is a real <button> with an accessible name', () => {
    const { el } = setup();
    const buttons = Array.from(el.querySelectorAll('.command-buttons button'));
    expect(buttons.length).toBe(6);
    for (const b of buttons) {
      expect(b.getAttribute('type')).toBe('button');
      expect((b.textContent ?? '').trim().length).toBeGreaterThan(0);
    }
  });
});
