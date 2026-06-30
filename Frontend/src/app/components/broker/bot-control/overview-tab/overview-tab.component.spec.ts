import { CommonModule } from '@angular/common';
import { Component, Directive, Input, provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import {
  makeLifecycleChartFixture,
  makeOperatorSurfaceFixture,
} from '../../../../testing/live-instance-status-fixtures';
import { OverviewTabComponent } from './overview-tab.component';
import { TraderGuidancePaneComponent } from './trader-guidance-pane.component';

@Component({
  // eslint-disable-next-line @angular-eslint/component-selector
  selector: 'vflow',
  standalone: true,
  template: '<ng-content />',
})
class VflowStubComponent {
  @Input() nodes: unknown;
  @Input() edges: unknown;
  @Input() view: unknown;
  @Input() background: unknown;
  @Input() minZoom: unknown;
  @Input() maxZoom: unknown;
  @Input() entitiesSelectable: unknown;
}

@Component({
  // eslint-disable-next-line @angular-eslint/component-selector
  selector: 'handle',
  standalone: true,
  template: '',
})
class HandleStubComponent {
  @Input() id: unknown;
  @Input() type: unknown;
  @Input() position: unknown;
}

@Directive({
  // eslint-disable-next-line @angular-eslint/directive-selector
  selector: 'ng-template[nodeHtml]',
  standalone: true,
})
class NodeHtmlStubDirective {}

@Directive({
  // eslint-disable-next-line @angular-eslint/directive-selector
  selector: 'ng-template[edge]',
  standalone: true,
})
class EdgeStubDirective {}

@Directive({
  // eslint-disable-next-line @angular-eslint/directive-selector
  selector: 'g[customTemplateEdge]',
  standalone: true,
})
class CustomTemplateEdgeStubDirective {}

const OVERVIEW_TEST_IMPORTS = [
  CommonModule,
  VflowStubComponent,
  HandleStubComponent,
  NodeHtmlStubDirective,
  EdgeStubDirective,
  CustomTemplateEdgeStubDirective,
  TraderGuidancePaneComponent,
];

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
    },
    {
      id: 'active_to_recovery',
      source: 'active',
      target: 'recovery',
      status: 'blocked',
      label: 'Safety incident',
      animated: false,
    },
  ];
  return status;
}

describe('OverviewTabComponent', () => {
  it('keeps the global lifecycle on a vertical path with themed blocked edges', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const status = makeStatus();
    status.lifecycle_chart.global_graph.nodes[0].status_label = 'Server clear';
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();

    expect(renderedText(fixture)).toContain('Deploy or start · Server clear');
    const points = new Map(fixture.componentInstance.nodes().map((node) => [node.id, node.point()]));
    expect(points.get('deploy')?.x).toBe(points.get('preflight')?.x);
    expect(points.get('deploy')?.y).toBeLessThan(points.get('preflight')?.y ?? 0);
    expect(points.get('preflight')?.y).toBeLessThan(points.get('account_safety')?.y ?? 0);
    expect(points.get('submit_order')?.x).toBeGreaterThan(points.get('active')?.x ?? 0);
    expect(fixture.componentInstance.edgeColor('blocked')).toBe('var(--warn)');
  });

  it('expands an expandable backend-authored subgraph and collapses to global', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
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
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
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

  it('renders backend-authored trader guidance and emits its remediation action', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const actionSelected = vi.fn();
    const surface = makeOperatorSurfaceFixture({
      submit_readiness: {
        ...makeOperatorSurfaceFixture().submit_readiness,
        code: 'broker_state_unproven',
        label: 'Broker state unproven',
        can_submit: false,
        blocking_reason_codes: ['RECONCILIATION_NOT_AVAILABLE'],
      },
      trader_guidance: {
        ...makeOperatorSurfaceFixture().trader_guidance,
        situation_code: 'broker_state_unproven',
        headline: 'Broker state is not proven enough to submit.',
        primary_remediation: {
          kind: 'invoke_endpoint',
          endpoint: 'reconcile_instance',
          method: 'POST',
          path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
        },
      },
    });
    fixture.componentRef.setInput('status', {
      ...makeStatus(),
      operator_surface: surface,
    });
    fixture.componentInstance.traderGuidanceAction.subscribe(actionSelected);
    fixture.detectChanges();

    expect(renderedText(fixture)).toContain('Broker state is not proven enough to submit.');
    const button = fixture.nativeElement.querySelector(
      '[data-testid="trader-guidance-primary-remediation"]',
    ) as HTMLButtonElement | null;
    expect(button?.textContent).toContain('Reconcile now');
    button?.click();
    expect(actionSelected).toHaveBeenCalledWith(surface.trader_guidance.primary_remediation);
  });

  it('anchors active branch edges to distinct source handles', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', statusWithGlobalBranchEdges());
    fixture.detectChanges();

    const edges = new Map(fixture.componentInstance.edges().map((edge) => [edge.id, edge]));
    expect(edges.get('active_to_submit_order')?.sourceHandle).toBe('s-right');
    expect(edges.get('active_to_submit_order')?.targetHandle).toBe('t-left');
    expect(edges.get('active_to_recovery')?.sourceHandle).toBe('s-bottom');
    expect(edges.get('active_to_recovery')?.targetHandle).toBe('t-top');
    expect(edges.get('active_to_submit_order')?.sourceHandle)
      .not.toBe(edges.get('active_to_recovery')?.sourceHandle);
  });

  it('returns to the global graph when the bot identity changes', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
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
