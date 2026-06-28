import { CommonModule } from '@angular/common';
import { Component, Directive, Input, provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import { OverviewActionsComponent } from './overview-actions.component';
import { OverviewTabComponent } from './overview-tab.component';

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
  OverviewActionsComponent,
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
    operator_surface: {} as LiveInstanceStatus['operator_surface'],
    lifecycle_chart: makeLifecycleChartFixture({
      selected_bot_id: id,
    }),
    fetched_at_ms: 0,
  };
}

function renderedText(fixture: { nativeElement: HTMLElement }): string {
  return fixture.nativeElement.textContent?.replace(/\s+/g, ' ').trim() ?? '';
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
    fixture.componentRef.setInput('status', makeStatus());
    fixture.detectChanges();

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

  it('emits the backend-authored action id when an enabled action is clicked', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });
    TestBed.overrideComponent(OverviewTabComponent, {
      set: { imports: OVERVIEW_TEST_IMPORTS },
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const actionInvoked = vi.fn();
    fixture.componentRef.setInput('status', makeStatus());
    fixture.componentInstance.actionInvoked.subscribe(actionInvoked);
    fixture.detectChanges();

    const action = fixture.nativeElement.querySelector('.chart-action') as HTMLButtonElement;
    action.click();
    expect(actionInvoked).toHaveBeenCalledWith('start_process');
  });
});
