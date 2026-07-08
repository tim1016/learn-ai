import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { LifecycleChartNode, LiveInstanceStatus } from '../../../api/live-instances.types';
import { makeStatus } from './bot-control-page.fixtures';
import { BotControlSidePanelComponent } from './bot-control-side-panel.component';
import { NodeInspectorComponent } from './node-inspector.component';
import { BotEventStreamComponent } from './reused/bot-event-stream/bot-event-stream.component';

@Component({
  selector: 'app-bot-event-stream',
  template: '<div data-testid="bot-event-stream-stub">{{ runId() }}</div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class BotEventStreamStubComponent {
  readonly runId = input.required<string>();
}

@Component({
  selector: 'app-node-inspector',
  template: '<div data-testid="node-inspector-stub">{{ node().label }}</div>',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
class NodeInspectorStubComponent {
  readonly node = input.required<LifecycleChartNode>();
  readonly status = input.required<LiveInstanceStatus>();
  readonly hasExplicitSelection = input<boolean>(false);
  readonly redeployRequested = output();
}

function primaryNode(status: LiveInstanceStatus): LifecycleChartNode {
  const graph = status.lifecycle_chart.global_graph;
  const node = graph.nodes.find((candidate) => candidate.id === graph.primary_node_id);
  if (!node) throw new Error('Expected primary lifecycle node in fixture.');
  return node;
}

function render(status: LiveInstanceStatus) {
  TestBed.overrideComponent(BotControlSidePanelComponent, {
    remove: { imports: [BotEventStreamComponent, NodeInspectorComponent] },
    add: { imports: [BotEventStreamStubComponent, NodeInspectorStubComponent] },
  });
  const fixture = TestBed.createComponent(BotControlSidePanelComponent);
  fixture.componentRef.setInput('status', status);
  fixture.componentRef.setInput('node', primaryNode(status));
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
