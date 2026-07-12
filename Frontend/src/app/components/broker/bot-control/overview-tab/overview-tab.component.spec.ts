import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import {
  makeDailyLifecycleFixture,
  makeLifecycleChartFixture,
} from '../../../../testing/live-instance-status-fixtures';
import { makeOperatorSurfaceFixture } from '../../../../testing/operator-surface-fixtures';
import { OverviewTabComponent } from './overview-tab.component';

function makeStatus(id = 'sid-x'): LiveInstanceStatus {
  return {
    stream_epoch: 'fixture-epoch',
    surface_version: 1,
    strategy_instance_id: id,
    process: { state: 'idle', pid: null, bound_run_id: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
    latest_mutation: null,
    desired_state: null,
    readiness: null,
    latest_decision: null,
    latest_signal_tone: 'neutral',
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
    daily_lifecycle: makeDailyLifecycleFixture(),
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

function statusWithFullLifecycleEdges(): LiveInstanceStatus {
  const status = makeStatus();
  status.lifecycle_chart.global_graph.edges = [
    ['deploy_to_preflight', 'deploy', 'preflight'],
    ['preflight_to_account_safety', 'preflight', 'account_safety'],
    ['account_safety_to_reconcile', 'account_safety', 'reconcile'],
    ['reconcile_to_activate', 'reconcile', 'activate'],
    ['activate_to_active', 'activate', 'active'],
    ['active_to_submit_order', 'active', 'submit_order'],
    ['active_to_recovery', 'active', 'recovery'],
    ['submit_order_to_broker_writer', 'submit_order', 'broker_writer'],
  ].map(([id, source, target]) => ({
    id,
    source,
    target,
    status: 'inactive' as const,
    label: null,
    animated: false,
    source_handle: null,
    target_handle: null,
  }));
  return status;
}

describe('OverviewTabComponent', () => {
  it('renders every global lifecycle gate as compact horizontal JointJS control nodes with themed blocked states', () => {
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
    expect(el.querySelector('app-lifecycle-joint-board')).not.toBeNull();
    const gateLabels = Array.from(el.querySelectorAll<HTMLElement>('.joint-node-shell .flow-node strong'))
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
    const nodePositions = Array.from(el.querySelectorAll<HTMLElement>('.joint-node-shell'))
      .map((node) => `${node.style.left}, ${node.style.top}`);
    expect(nodePositions).toEqual([
      '18px, 42px',
      '228px, 42px',
      '438px, 248px',
      '648px, 248px',
      '858px, 42px',
      '1068px, 42px',
      '1278px, 248px',
      '1488px, 248px',
      '1698px, 248px',
    ]);
    const blockedGate = el.querySelector<HTMLElement>('.flow-node.status-blocked');
    expect(blockedGate?.querySelector('strong')?.textContent?.trim()).toBe('Reconcile broker state');
  });

  it('places same-column nodes only when they share a branching parent', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', statusWithFullLifecycleEdges());
    fixture.detectChanges();

    const positions = new Map(
      Array.from((fixture.nativeElement as HTMLElement).querySelectorAll<HTMLElement>('.joint-node-shell'))
        .map((node) => [
          node.querySelector('.flow-node strong')?.textContent?.trim(),
          `${node.style.left}, ${node.style.top}`,
        ]),
    );
    expect(positions.get('Monitor live bot')).toBe('1068px, 42px');
    expect(positions.get('Submit order path')).toBe('1278px, 42px');
    expect(positions.get('Recovery lane')).toBe('1278px, 248px');
    expect(positions.get('Broker activity')).toBe('1488px, 42px');
  });

  it('marks the blocking node while preserving backend-authored edge status', () => {
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
    expect(el.querySelector<HTMLElement>('.lifecycle-joint-edge-summary.status-blocked')).toBeNull();
    const waitingConnector = el.querySelector<HTMLElement>('.lifecycle-joint-edge-summary.status-inactive');
    expect(waitingConnector?.textContent?.trim()).toBe('Waiting');
  });

  it('renders the backend-authored lifecycle signals as a compact strip', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const status = makeStatus();
    status.operator_surface = makeOperatorSurfaceFixture({
      blockage_ladder: {
        headline: 'IBKR data farm degraded',
        summary: 'The live broker data farm is degraded while this bot depends on broker market data.',
        current_stage_id: 'broker',
        stages: [
          {
            id: 'control_plane',
            label: 'Control plane',
            state: 'clear',
            severity: 'ok',
            current: false,
            title: 'Daemon control plane connected',
            summary: 'The data plane can reach the host live-runner daemon.',
            next_step: null,
            reason_codes: [],
          },
          {
            id: 'broker',
            label: 'Broker proof',
            state: 'danger',
            severity: 'critical',
            current: true,
            title: 'IBKR data farm degraded',
            summary: 'The live broker data farm is degraded while this bot depends on broker market data.',
            next_step: 'Do not submit new orders until IBKR market-data evidence is healthy again.',
            reason_codes: ['BROKER_DATA_FARM_DEGRADED'],
          },
        ],
      },
    });
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();

    const text = renderedText(fixture);
    expect(text).toContain('Signals');
    expect(text).toContain('IBKR data farm degraded');
    expect(text).toContain('Broker proof');
    const current = (fixture.nativeElement as HTMLElement).querySelector<HTMLElement>('.blockage-step.current');
    expect(current?.textContent).toContain('Broker proof');
    expect(current?.textContent).toContain('IBKR data farm degraded');
    expect(current?.classList.contains('severity-critical')).toBe(true);
  });

  it('emits a recovery override request when crash recovery blocks Start', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const requested = vi.fn();
    const status = makeStatus();
    status.operator_surface.host_process.start_capability = {
      enabled: false,
      run_id: null,
      request: null,
      disabled_reason_code: 'CRASH_RECOVERY_REQUIRED',
      gate_results: [
        {
          gate_id: 'account.crash_recovery',
          status: 'block',
          source: 'account_instance_registry',
          operator_reason: 'CRASH_RECOVERY_REQUIRED',
          operator_next_step: 'Record audited recovery evidence.',
          evidence_at_ms: 1_700_000_000_000,
        },
      ],
    };
    fixture.componentRef.setInput('status', status);
    fixture.componentInstance.crashRecoveryOverrideRequested.subscribe(requested);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('.recovery-button');
    expect(button?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Record recovery override');
    button?.click();
    expect(requested).toHaveBeenCalledTimes(1);

    fixture.componentRef.setInput('recoveryOverrideBusy', true);
    fixture.detectChanges();
    button?.click();
    expect(requested).toHaveBeenCalledTimes(1);
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

    const el = fixture.nativeElement as HTMLElement;
    el.querySelector<HTMLButtonElement>('[aria-label^="Open Deploy or start details"]')?.click();
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Deploy and start internals');
    expect(renderedText(fixture)).toContain('Host state');

    fixture.componentInstance.collapse();
    fixture.detectChanges();
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');
  });

  it('vertically collapses and restores the lifecycle flow without hiding signals', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', makeStatus());
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const toggle = el.querySelector<HTMLButtonElement>('[aria-label="Hide bot flow chart"]');
    const flowPanel = el.querySelector<HTMLElement>('#lifecycle-flow-panel');
    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(flowPanel?.hidden).toBe(false);
    expect(el.querySelector('.blockage-ladder')?.textContent).toContain('Signals');

    toggle?.click();
    fixture.detectChanges();

    expect(flowPanel?.hidden).toBe(true);
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    expect(toggle?.getAttribute('aria-label')).toBe('Show bot flow chart');
    expect(el.querySelector('.blockage-ladder')?.textContent).toContain('Signals');

    toggle?.click();
    fixture.detectChanges();

    expect(flowPanel?.hidden).toBe(false);
    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(toggle?.getAttribute('aria-label')).toBe('Hide bot flow chart');
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

    const el = fixture.nativeElement as HTMLElement;
    el.querySelector<HTMLButtonElement>('[aria-label^="Select Monitor live bot"]')?.click();
    expect(nodeSelected).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'active' }),
    );
    expect(renderedText(fixture)).toContain('Bot lifecycle overview');
  });

  it('expands node receipts with a dedicated header button and one open region', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    const status = makeStatus();
    const deploy = status.lifecycle_chart.global_graph.nodes[0];
    const preflight = status.lifecycle_chart.global_graph.nodes[1];
    deploy.receipts = [
      {
        label: 'deploy.state',
        value: 'READY',
        headline: 'Deploy gate is ready.',
        detail: 'The host start gate is ready.',
        unit: null,
        source: 'operator_surface',
        gate_id: 'desired_state.start',
        ts_ms: 1_700_000_000_000,
        ts_ms_resolved: true,
      },
    ];
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const deployNode = el.querySelector<HTMLElement>('app-lifecycle-node-card .flow-node');
    expect(deployNode?.getAttribute('role')).toBeNull();

    let deployToggle = el.querySelector<HTMLButtonElement>(
      '[aria-controls="lifecycle-node-receipts-global-deploy"]',
    );
    expect(deployToggle?.getAttribute('aria-expanded')).toBe('false');
    deployToggle?.click();
    fixture.detectChanges();
    deployToggle = el.querySelector<HTMLButtonElement>(
      '[aria-controls="lifecycle-node-receipts-global-deploy"]',
    );

    const deployReceipts = el.querySelector<HTMLElement>('[data-testid="lifecycle-node-receipts-deploy"]');
    expect(deployToggle?.getAttribute('aria-expanded')).toBe('true');
    expect(deployReceipts?.textContent).toContain('Deploy gate is ready.');
    expect(deployReceipts?.textContent).toContain('Deploy State is Ready.');
    expect(deployReceipts?.querySelector(':scope > app-node-receipts-list')).not.toBeNull();
    expect(deployReceipts?.querySelector(':scope > details')).toBeNull();

    let preflightToggle = el.querySelector<HTMLButtonElement>(
      '[aria-controls="lifecycle-node-receipts-global-preflight"]',
    );
    preflightToggle?.click();
    fixture.detectChanges();
    preflightToggle = el.querySelector<HTMLButtonElement>(
      '[aria-controls="lifecycle-node-receipts-global-preflight"]',
    );

    expect(el.querySelector('[data-testid="lifecycle-node-receipts-deploy"]')).toBeNull();
    expect(el.querySelector('[data-testid="lifecycle-node-receipts-preflight"]')?.textContent)
      .toContain('No node-scoped receipts have been emitted');
    expect(preflightToggle?.getAttribute('aria-expanded')).toBe('true');
    expect(preflight.id).toBe('preflight');
  });

  it('renders backend-authored branch transitions as horizontal JointJS arrows', () => {
    TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideZonelessChangeDetection()],
    });

    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('status', statusWithGlobalBranchEdges());
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-lifecycle-joint-board')).not.toBeNull();
    const edgeTitles = Array.from(el.querySelectorAll<HTMLElement>('.lifecycle-joint-edge-summary'))
      .map((edge) => edge.textContent?.trim());
    expect(edgeTitles).toContain('Active: Signal arrives');
    expect(edgeTitles).toContain('Blocked: Safety incident');
    const animatedConnector = el.querySelector<HTMLElement>('.lifecycle-joint-edge-summary.edge-animated');
    expect(animatedConnector?.textContent?.trim()).toBe('Active: Signal arrives');
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
