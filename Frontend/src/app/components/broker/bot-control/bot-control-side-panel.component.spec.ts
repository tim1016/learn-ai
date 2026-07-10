import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import { makeStatus } from './bot-control-page.fixtures';
import { BotControlSidePanelComponent } from './bot-control-side-panel.component';
import { BotEventStreamComponent } from './reused/bot-event-stream/bot-event-stream.component';
import type { BotEventStreamCommand } from './reused/bot-event-stream/bot-event-stream-action';

@Component({
  selector: 'app-bot-event-stream',
  template: '<div data-testid="bot-event-stream-stub">{{ runId() }}</div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class BotEventStreamStubComponent {
  readonly runId = input.required<string>();
  readonly status = input.required<LiveInstanceStatus>();
  readonly commandsDisabled = input(false);
  readonly actionInvoked = output<BotEventStreamCommand>();
}

function render(status: LiveInstanceStatus) {
  TestBed.overrideComponent(BotControlSidePanelComponent, {
    remove: { imports: [BotEventStreamComponent] },
    add: { imports: [BotEventStreamStubComponent] },
  });
  const fixture = TestBed.createComponent(BotControlSidePanelComponent);
  fixture.componentRef.setInput('status', status);
  fixture.detectChanges();
  return fixture;
}

afterEach(() => TestBed.resetTestingModule());

describe('BotControlSidePanelComponent', () => {
  it('binds the side-panel stream to the live run before the evidence run', () => {
    const status = makeStatus();
    status.live_binding = { run_id: 'run-live', run_dir: null, source: 'registry' };
    status.evidence_binding = { run_id: 'run-evidence', state: 'latest_run_by_ledger', is_live: false };

    const fixture = render(status);
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[data-testid="bot-event-stream-stub"]')?.textContent)
      .toContain('run-live');
    expect(el.querySelector('[data-testid="bot-event-stream-no-run"]')).toBeNull();
    expect(el.querySelector('app-node-inspector')).toBeNull();
  });

  it('falls back to the evidence run when no live run is bound', () => {
    const status = makeStatus();
    status.evidence_binding = { run_id: 'run-evidence', state: 'latest_run_by_ledger', is_live: false };

    const fixture = render(status);
    const el = fixture.nativeElement as HTMLElement;

    expect(el.querySelector('[data-testid="bot-event-stream-stub"]')?.textContent)
      .toContain('run-evidence');
  });

  it('renders an honest no-run state that emits Fresh run', () => {
    const fixture = render(makeStatus());
    let emitted = 0;
    fixture.componentInstance.freshRunRequested.subscribe(() => emitted += 1);
    const el = fixture.nativeElement as HTMLElement;

    const empty = el.querySelector<HTMLElement>('[data-testid="bot-event-stream-no-run"]');
    expect(empty?.textContent).toContain('No run bound yet');

    empty?.querySelector<HTMLButtonElement>('button')?.click();
    fixture.detectChanges();
    expect(emitted).toBe(1);
  });
});
