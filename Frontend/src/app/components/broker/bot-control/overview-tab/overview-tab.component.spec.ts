import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import { makeOperatorSurfaceFixture } from '../../../../testing/operator-surface-fixtures';
import { OverviewTabComponent } from './overview-tab.component';

function makeStatus(id = 'sid-x'): LiveInstanceStatus {
  return {
    strategy_instance_id: id,
    process: { state: 'idle', pid: null, bound_run_id: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
    desired_state: null,
    readiness: null,
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: null,
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: makeOperatorSurfaceFixture(),
    lifecycle_chart: makeLifecycleChartFixture({
      selected_bot_id: id,
    }),
    fetched_at_ms: 0,
  };
}

function renderedText(fixture: { nativeElement: HTMLElement }): string {
  return fixture.nativeElement.textContent?.replace(/\s+/g, ' ').trim() ?? '';
}

function statusWithGlobalBranchEdges(): LiveInstanceStatus {
  const status = makeStatus();
  status.lifecycle_chart.global_graph.edges = [
    {
      id: 'active_to_submit_order',
      source: 'active',
      target: 'submit_order',
      status: 'active',
      label: 'Signal arrives',
      animated: true,
      source_handle: 'source-east',
      target_handle: 'target-west',
    },
    {
      id: 'active_to_recovery',
      source: 'active',
      target: 'recovery',
      status: 'blocked',
      label: 'Safety incident',
      animated: false,
      source_handle: 'source-south',
      target_handle: 'target-north',
    },
  ];
  return status;
}

describe('OverviewTabComponent', () => {
  it('renders every global lifecycle gate as document-flow cards with themed blocked states', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const status = makeStatus();
    status.lifecycle_chart.global_graph.nodes[0].status_label = 'Server clear';
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();

    expect(renderedText(fixture)).toContain('Deploy or start · Server clear');
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('vflow')).toBeNull();
    const gateLabels = Array.from(el.querySelectorAll<HTMLElement>('.flow-node strong'))
      .map((node) => node.textContent?.trim());
    expect(gateLabels).toEqual([
      'Deploy or start',
      'Pre-flight gates',
      'Account safety',
      'Reconcile broker state',
      'Activate bot',
      'Monitor live bot',
      'Submit order path',
      'Broker activity',
      'Recovery lane',
    ]);
    const blockedGate = el.querySelector<HTMLElement>('.flow-node.status-blocked');
    expect(blockedGate?.querySelector('strong')?.textContent?.trim()).toBe('Reconcile broker state');
  });

  it('marks the current blocking gate directly in the lifecycle flow', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const status = makeStatus();
    status.lifecycle_chart.global_graph.primary_node_id = 'deploy';
    status.lifecycle_chart.global_graph.nodes[0].status = 'blocked';
    status.lifecycle_chart.global_graph.nodes[0].status_label = 'Blocked';
    status.lifecycle_chart.global_graph.edges = [
      {
        id: 'deploy_to_preflight',
        source: 'deploy',
        target: 'preflight',
        status: 'inactive',
        label: null,
        animated: false,
        source_handle: null,
        target_handle: null,
      },
    ];
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const blockingNode = el.querySelector<HTMLElement>('.flow-node.blocking-node');
    expect(blockingNode?.querySelector('strong')?.textContent?.trim()).toBe('Deploy or start');
    expect(blockingNode?.querySelector('.node-callout')?.textContent?.trim()).toBe('Blocking step');
    const blockedConnector = el.querySelector<HTMLElement>('.flow-connector.status-blocked');
    expect(blockedConnector?.textContent?.replace(/\s+/g, ' ')).toContain(
      'Blocked Deploy or start -> Pre-flight gates',
    );
  });

  it('expands an expandable backend-authored subgraph and collapses to global', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', makeStatus());
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');

    const node = fixture.componentInstance.chart().global_graph.nodes[0];
    fixture.componentInstance.expandNode(node);
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Deploy and start internals');
    expect(renderedText(fixture)).toContain('Host state');

    fixture.componentInstance.collapse();
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');
  });

  it('emits the selected lifecycle node when a graph node is activated', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const nodeSelected = vi.fn();
    fixture.componentRef.setInput('status', makeStatus());
    fixture.componentInstance.nodeSelected.subscribe(nodeSelected);
    fixture.detectChanges();

    const node = fixture.componentInstance.chart().global_graph.nodes.find((candidate) => !candidate.expandable);
    if (!node) throw new Error('Expected a non-expandable lifecycle node in fixture.');
    fixture.componentInstance.expandNode(node);
    expect(nodeSelected).toHaveBeenCalledWith(node);
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');
  });

  it('renders backend-authored branch transitions as in-flow arrows without requiring a pan-zoom viewport', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', statusWithGlobalBranchEdges());
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('vflow')).toBeNull();
    const transitionText = el.querySelector('.connector-group')?.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(transitionText).toContain('Active Monitor live bot -> Submit order path Signal arrives');
    expect(transitionText).toContain('Blocked Monitor live bot -> Recovery lane Safety incident');
    const animatedConnector = el.querySelector<HTMLElement>('.flow-connector.connector-animated');
    expect(animatedConnector?.textContent?.replace(/\s+/g, ' ')).toContain('Signal arrives');
  });

  it('returns to the global graph when the bot identity changes', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', makeStatus('sid-x'));
    fixture.detectChanges();

    fixture.componentInstance.expandNode(fixture.componentInstance.chart().global_graph.nodes[0]);
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Deploy and start internals');

    fixture.componentRef.setInput('status', makeStatus('sid-y'));
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');
  });

});
