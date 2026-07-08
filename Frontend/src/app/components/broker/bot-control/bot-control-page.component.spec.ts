import { HttpErrorResponse } from '@angular/common/http';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LifecycleTimelineResponse } from '../../../api/live-instances.types';
import {
  allowFlattenAndPause,
  makeCommandWriteResponse,
  makeDesiredStateResponse,
  makeHostRunnerHealth,
  makeIncidentHeadline,
  makeLifecycleTimeline,
  makeReconcileAckResponse,
  makeRuntimeFreshnessWithLeaseAction,
  makeStatus,
} from './bot-control-page.fixtures';
import {
  deferred,
  flush,
  installBotControlPageTestStubs,
  setupBotControlPage,
} from './bot-control-page.testing';

describe('BotControlPageComponent', () => {
  beforeEach(() => {
    installBotControlPageTestStubs();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  function openLifecycleReceipts(
    fixture: { detectChanges(): void },
    element: HTMLElement,
    graphId: string,
    nodeId: string,
  ): HTMLElement {
    const toggle = element.querySelector<HTMLButtonElement>(
      `[aria-controls="lifecycle-node-receipts-${graphId}-${nodeId}"]`,
    );
    expect(toggle).not.toBeNull();
    if (!toggle) throw new Error(`Expected receipts toggle for ${graphId}/${nodeId}.`);
    toggle.click();
    fixture.detectChanges();

    const receipts = element.querySelector<HTMLElement>(`[data-testid="lifecycle-node-receipts-${nodeId}"]`);
    expect(receipts).not.toBeNull();
    if (!receipts) throw new Error(`Expected receipts region for ${nodeId}.`);
    return receipts;
  }

  function lifecycleActionTitle(button: HTMLButtonElement | null): string {
    expect(button).not.toBeNull();
    if (!button) throw new Error('Expected lifecycle action button.');
    return button.closest<HTMLElement>('.chart-action-shell')?.getAttribute('title')
      ?? button.getAttribute('title')
      ?? '';
  }

  it('renders compact broker evidence and control-plane warning panels before the bot tabs', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { element: el } = await setupBotControlPage();
    const brokerPanel = el.querySelector('[data-testid="bot-control-broker-evidence-banner"]');
    const controlPanel = el.querySelector('[data-testid="bot-control-plane-banner"]');
    expect(brokerPanel?.textContent)
      .toContain('Warning, broker evidence unavailable.');
    expect(controlPanel?.textContent)
      .toContain('Control plane, last known.');
    const runbookLinks = Array.from(el.querySelectorAll<HTMLAnchorElement>('.warning-link'))
      .map((link) => link.getAttribute('href'));
    expect(runbookLinks).toContain('/broker/account-monitor');
    expect(runbookLinks).toContain('/broker');
    expect(runbookLinks).not.toContain('/data-lab');
    const dismissButtons = Array.from(el.querySelectorAll<HTMLButtonElement>('.slim-dismiss'));
    expect(dismissButtons.map((button) => button.getAttribute('title'))).toEqual([
      'Dismiss broker evidence warning',
      'Dismiss control plane warning',
    ]);
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-tabs"]')).toBeNull();
    expect(el.querySelector('.decision-row')).toBeNull();
  });

  it('renders the backend-authored broker connection condition in the header', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.broker.connection = 'DEGRADED';
    status.operator_surface.broker.connection_condition = {
      code: 'BROKER_RECOVERING',
      severity: 'warning',
      title: 'Broker recovering streams',
      summary: 'The broker link is back, but runtime stream recovery is still underway.',
      remediation: 'Wait for recovery probes and subscriptions to pass before submitting orders.',
    };

    const { element: el } = await setupBotControlPage({ status });

    const pill = el.querySelector('.connection-pill');
    expect(pill?.textContent).toContain('Broker recovering streams');
    expect(pill?.textContent).not.toContain('Degraded');
  });

  it('renders backend-authored runtime incident headlines on the bot control page', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.incident_headline = makeIncidentHeadline();
    const { element: el } = await setupBotControlPage({ status });

    const incident = el.querySelector('[data-testid="runtime-banner-incident"]');
    expect(incident).not.toBeNull();
    expect(incident?.textContent).toContain('Flatten timed out');
    expect(incident?.textContent).toContain(
      'The watchdog could not prove that the account is flat after the emergency flatten attempt.',
    );
  });

  it('runs the backend-authored renew-lease action from runtime freshness notices', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.runtime_freshness = makeRuntimeFreshnessWithLeaseAction();
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status,
      mutationResponses: { renewControlPlaneLease: makeHostRunnerHealth() },
    });

    const action = element.querySelector<HTMLButtonElement>(
      '[data-testid="operator-notice-action"]',
    );
    expect(action?.textContent).toContain('Renew control-plane lease');
    action?.click();
    fixture.detectChanges();
    await flush(fixture);

    expect(liveRuns.renewControlPlaneLease).toHaveBeenCalledTimes(1);
  });

  it('folds attention copy into the lifecycle overview without a dropdown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { element: el } = await setupBotControlPage();

    expect(el.querySelector('[data-testid="bot-control-attention-toggle"]')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-attention-panel"]')).toBeNull();
    expect(el.textContent).toContain('Broker session is disconnected');
    expect(el.textContent).toContain('Reconnect the broker session, then refresh broker evidence.');
  });

  it('renders critical attention groups inline in the lifecycle overview', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.trader_guidance.additional_attention_groups = [
      {
        code: 'broker_safety',
        severity: 'critical',
        headline: 'Broker safety is unsafe',
        explanation: 'Paper-safety evidence is unsafe.',
        operator_next_step: 'Inspect broker/account safety evidence before any trading action.',
        remediation: { kind: 'open_runbook', slug: 'broker-instance-operator-surface' },
      },
    ];
    const { element } = await setupBotControlPage({ status });

    expect(element.querySelector('[data-testid="bot-control-attention-panel"]')).toBeNull();
    expect(element.textContent).toContain('Broker safety is unsafe');
    expect(element.textContent).toContain('Inspect broker/account safety evidence before any trading action.');
  });

  it('keeps lifecycle overview visible and switches the right pane from selected chart nodes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus({
      runSignal: {
        state_label: 'Off',
        tone: 'off',
        title: 'Bot process is not running',
        detail: 'Start the host runner before trading this bot.',
      },
    });
    const { fixture, component, element: el } = await setupBotControlPage({ status });
    expect(el.querySelector('[data-testid="bot-run-signal"]')?.textContent)
      .toContain('Off');
    expect(el.querySelector('.top-action-banner')?.textContent).toContain('Controls');
    const startAction = el.querySelector(
      '.top-action-banner .chart-action[aria-label="Start bot process"]',
    ) as HTMLButtonElement | null;
    expect(startAction).not.toBeNull();
    expect(startAction?.textContent?.trim()).toBe('Start bot process');
    expect(el.querySelector('app-overview-tab')).not.toBeNull();
    expect(el.querySelector('app-overview-tab app-trader-guidance-pane')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Current lifecycle focus');
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Deploy or start');

    component.setActiveWorkbenchTab('audit');
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="locked-evidence-field"]')).not.toBeNull();

    const dispatch = vi.spyOn(component, 'dispatchOverviewAction');
    startAction?.click();
    expect(dispatch).toHaveBeenCalledWith('start_process');

    const recovery = component.status()
      ?.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    expect(recovery).toBeDefined();
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    recovery.operator_actionability = 'system-only';
    component.selectLifecycleNode(recovery);
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Selected lifecycle step');
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Recovery lane');
    expect(el.textContent).toContain('Internal gate - no operator action needed');
    component.selectedLifecycleNodeId.set(null);
    fixture.detectChanges();
    recovery.operator_actionability = 'operator-actionable';
    component.selectLifecycleNode(recovery);
    fixture.detectChanges();
    expect(el.textContent).toContain('Operator action is required for this lifecycle step.');
    expect(el.querySelector('[data-testid="bot-control-tabs"]')).toBeNull();
  });

  it('renders bot runtime as compact on/off signals beside one-click controls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const stopped = makeStatus({
      hostState: 'IDLE',
      runSignal: {
        state_label: 'Off',
        tone: 'off',
        title: 'Bot process is not running',
        detail: 'The host is reachable but this bot has no active process. Start it to resume trading.',
      },
    });
    stopped.operator_surface.blockage_ladder = {
      headline: 'Bot process is not running',
      summary: 'The host is reachable but this bot has no active process. Start it to resume trading.',
      current_stage_id: 'host_process',
      stages: [
        {
          id: 'host_process',
          label: 'Host process',
          state: 'warning',
          severity: 'warning',
          current: true,
          title: 'Bot process is not running',
          summary: 'The host is reachable but this bot has no active process. Start it to resume trading.',
          next_step: null,
          reason_codes: ['HOST_PROCESS_IDLE'],
        },
      ],
    };
    const { fixture, component, element } = await setupBotControlPage({ status: stopped });

    const offSignal = element.querySelector('[data-testid="bot-run-signal"]');
    expect(offSignal?.textContent).toContain('Bot');
    expect(offSignal?.textContent).toContain('Off');
    expect(offSignal?.textContent).toContain('Bot process is not running');
    expect(offSignal?.classList.contains('tone-off')).toBe(true);
    expect(element.querySelector('.chart-action[aria-label="Start bot process"]')).not.toBeNull();

    const running = makeStatus({
      hostState: 'RUNNING',
      runSignal: {
        state_label: 'On',
        tone: 'on',
        title: 'Bot process is running',
        detail: 'The host daemon reports this bot process is running.',
      },
    });
    component.status.set(running);
    fixture.detectChanges();

    const onSignal = element.querySelector('[data-testid="bot-run-signal"]');
    expect(onSignal?.textContent).toContain('On');
    expect(onSignal?.textContent).toContain('Bot process is running');
    expect(onSignal?.classList.contains('tone-on')).toBe(true);
  });

  it('renders human-labelled posture pills in the header and omits the execution pill', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    // execution posture is optional and must never be rendered as a header pill.
    status.operator_surface.execution = { posture: 'UNSAFE' };
    const { element: el } = await setupBotControlPage({ status });
    const header = el.querySelector('.header-strip');
    // Broker proof / Submit / Exposure pills render as human labels.
    expect(header?.textContent).toContain('Broker proof');
    expect(header?.textContent).toContain('Submit');
    expect(header?.textContent).toContain('Exposure');
    // submit_readiness.label is backend prose; safety_verdict/posture UNKNOWN pipe to "Unknown".
    expect(header?.textContent).toContain('Broker state unproven');
    expect(header?.textContent).toContain('Unknown');
    // No fourth "Execution" pill and no raw enum codes in the header.
    expect(header?.textContent).not.toContain('Execution');
    expect(header?.textContent).not.toContain('UNSAFE');
    expect(header?.textContent).not.toContain('PAPER_ONLY');
    expect(header?.textContent).not.toContain('FLAT');
  });

  it('renders backend-authored disabled action prose in the action tooltip', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.lifecycle_chart.actions = [
      {
        id: 'flatten_and_pause',
        label: 'Flatten and pause',
        enabled: false,
        reason_code: 'NO_LIVE_BINDING',
        reason_headline: 'No live binding',
        reason_detail: 'The lifecycle action contract says the runner is not bound.',
        target_node_id: 'recovery',
        tone: 'danger',
      },
    ];
    status.operator_surface.actions.flatten_and_pause = {
      enabled: false,
      effect: 'LIVE_ACTUATION',
      disabled_reason_code: 'BROKER_SAFETY_UNSAFE',
      disabled_reasons: ['BROKER_SAFETY_UNSAFE'],
      gate_results: [],
    };
    const { element: el } = await setupBotControlPage({ status });
    const actionButton = el.querySelector<HTMLButtonElement>('[aria-label="Flatten and pause"]');
    expect(actionButton?.disabled).toBe(true);
    const title = lifecycleActionTitle(actionButton);
    expect(title).toContain('No live binding');
    expect(title).toContain('The lifecycle action contract says the runner is not bound.');
    expect(title).not.toContain('NO_LIVE_BINDING');
    const traderCopy = Array.from(el.querySelectorAll('[data-trader-copy]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    const receipts = Array.from(el.querySelectorAll('[data-receipt]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    expect(traderCopy).not.toContain('NO_LIVE_BINDING');
    expect(traderCopy).not.toContain('BROKER_SAFETY_UNSAFE');
    expect(receipts).not.toContain('NO_LIVE_BINDING');
    expect(receipts).not.toContain('BROKER_SAFETY_UNSAFE');
  });

  it('keeps node selection explanatory and never gates enabled emergency actions', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.lifecycle_chart.actions = [
      {
        id: 'pause',
        label: 'Pause',
        enabled: true,
        reason_code: null,
        reason_headline: 'Available',
        reason_detail: 'Backend gates currently allow this action.',
        target_node_id: 'active',
        tone: 'secondary',
      },
    ];
    const { fixture, component, element } = await setupBotControlPage({ status });

    const recovery = status.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    component.selectLifecycleNode(recovery);
    fixture.detectChanges();

    const pause = element.querySelector<HTMLButtonElement>('.chart-action[aria-label="Pause"]');
    expect(pause?.getAttribute('aria-disabled')).toBe('false');
    expect(lifecycleActionTitle(pause)).toContain('Pause On. Available');
    expect(pause?.textContent?.trim()).toBe('Pause');
  });

  it('renders redeploy settings as one concise row and hides raw strategy keys', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.start_defaults = {
      strategy: 'deployment_validation',
      readonly: true,
      hydrate_policy: 'optional',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
    };
    status.operator_surface.trader_guidance.advanced_evidence = [
      {
        label: 'strategy',
        value: 'deployment_validation',
        source: 'operator_surface',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
    ];
    const { element: el } = await setupBotControlPage({ status });

    const settings = Array.from(
      el.querySelectorAll('[data-testid="redeploy-setting-field"]'),
    );
    const orderMode = settings.find((field) => field.textContent?.includes('Order mode'));
    expect(orderMode?.textContent).toContain('Read-only observation');
    expect(orderMode?.getAttribute('title')).toContain('fresh redeploy');
    expect(el.querySelectorAll('[data-testid="redeploy-setting-field"]')).toHaveLength(5);
    expect(el.querySelectorAll('button.link-button')).toHaveLength(1);
    expect(el.textContent).not.toContain('deployment_validation');
  });

  it('renders backend-authored closed-session runtime proof as trader-friendly evidence', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.trading_session.phase = 'CLOSED';
    status.operator_surface.runtime_freshness = {
      posture_demoted: false,
      stale_reason_codes: ['BAR_LOOP_SESSION_CLOSED'],
      command_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      broker: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      bar_loop: { state: 'STALE', age_ms: 90_000, stale_reason_codes: ['BAR_LOOP_SESSION_CLOSED'] },
      control_plane: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      headline: null,
      additional_reasons: [],
    };
    status.operator_surface.trader_guidance.proof_lines =
      status.operator_surface.trader_guidance.proof_lines.map((line) =>
        line.id === 'runtime-freshness'
          ? {
              id: 'runtime-freshness',
              label: 'Runtime',
              message:
                'The bot is idle until the regular trading session opens. No trading decision is being made.',
              detail: 'Market closed',
              tone: 'neutral',
            }
          : line,
      );
    const { fixture, component, element } = await setupBotControlPage({ status });
    component.setActiveWorkbenchTab('audit');
    fixture.detectChanges();

    const runtimeField = Array.from(
      element.querySelectorAll('[data-testid="locked-evidence-field"]'),
    ).find((field) => field.textContent?.includes('Runtime'));
    expect(runtimeField?.textContent).toContain(
      'The bot is idle until the regular trading session opens. No trading decision is being made.',
    );
    expect(runtimeField?.getAttribute('title')).toContain('Market closed');
    expect(runtimeField?.classList.contains('tone-neutral')).toBe(true);
    expect(runtimeField?.classList.contains('tone-attention')).toBe(false);
    expect(runtimeField?.textContent).not.toContain('ATTENTION');
    expect(runtimeField?.textContent).not.toContain('FRESH');
    expect(runtimeField?.textContent).not.toContain('BAR_LOOP_SESSION_CLOSED');
  });

  it('renders the projection timeline below the fold as recent activity', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.account_owner = {
      account_id: 'DU1',
      generation: 4,
      phase: 'accepting',
      recorded_at_ms: 1_700_000_000_000,
      source: 'account_owner',
    };
    const { component, element, liveRuns } = await setupBotControlPage({ status });

    expect(liveRuns.getLifecycleTimeline).toHaveBeenCalledWith({
      account_id: 'DU1',
      strategy_instance_id: 'sid-x',
      run_id: null,
      limit: 5,
    });
    const tabs = element.querySelector('[data-testid="bot-control-workbench-tabs"]');
    expect(tabs?.textContent).toContain('Recent activity');
    expect(tabs?.textContent).toContain('Full audit trail');
    expect(component.activeWorkbenchTab()).toBe('activity');
    const timeline = element.querySelector(
      '[data-testid="bot-control-recent-activity"] [data-testid="trader-guidance-timeline"]',
    );
    expect(timeline?.textContent).toContain('Broker acknowledgment failed; submit outcome is uncertain.');
    expect(timeline?.textContent).toContain('broker_ack #7');
  });

  it('only instantiates the selected workbench tab body', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, component, element: el } = await setupBotControlPage();
    expect(el.querySelector('[data-testid="activity-tab-stub"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="workbench-audit-panel"]')).toBeNull();

    component.setActiveWorkbenchTab('audit');
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="activity-tab-stub"]')).toBeNull();
    expect(el.querySelector('[data-testid="workbench-audit-panel"]')).not.toBeNull();
  });

  it('clears lifecycle timeline rows when refreshed status changes run context', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const nextTimeline = deferred<LifecycleTimelineResponse>();
    const nextStatus = {
      ...makeStatus(),
      evidence_binding: { run_id: 'run-y', state: 'latest_run_by_ledger', is_live: false },
    };
    const { fixture, component, element } = await setupBotControlPage({
      statusSequence: [makeStatus(), nextStatus],
      lifecycleTimelineSequence: [makeLifecycleTimeline(), nextTimeline.promise],
    });

    expect(element.textContent).toContain('Broker acknowledgment failed; submit outcome is uncertain.');

    await (component as unknown as { refreshStatus(id: string): Promise<void> }).refreshStatus('sid-x');
    fixture.detectChanges();

    const text = element.textContent ?? '';
    expect(text).not.toContain('Broker acknowledgment failed; submit outcome is uncertain.');
    expect(text).toContain('Lifecycle projection is unavailable for this bot.');
  });

  it('renders selected lifecycle node freshness and receipts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    const reconcile = status.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'reconcile');
    if (!reconcile) throw new Error('Expected reconcile lifecycle node in fixture.');
    reconcile.ts_ms = 1_700_000_001_000;
    reconcile.ts_ms_resolved = true;
    reconcile.receipts = [
      {
        label: 'reconciliation.state',
        value: 'FAILED',
        headline: 'Reconciliation failed.',
        detail: 'The backend says broker and engine state do not agree.',
        unit: null,
        source: 'reconciliation_projection',
        gate_id: null,
        ts_ms: 1_700_000_001_000,
        ts_ms_resolved: true,
      },
      {
        label: 'failure_reason',
        value: 'Broker snapshot disagrees with the intent WAL.',
        headline: 'Reconciliation is blocked by a broker and engine mismatch.',
        detail: 'Broker snapshot disagrees with the intent WAL.',
        unit: null,
        source: 'reconciliation_projection',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
      {
        label: 'intent_id',
        value: 'intent-7',
        headline: 'Order intent intent-7 was recorded.',
        detail: 'Intent ids are preserved exactly for audit.',
        unit: null,
        source: 'readiness',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
    ];
    const { fixture, component, element } = await setupBotControlPage({ status });
    component.selectLifecycleNode(reconcile);
    fixture.detectChanges();

    const receipts = openLifecycleReceipts(fixture, element, 'global', 'reconcile');
    expect(element.textContent).toContain('Evidence checked');
    expect(element.textContent).toContain('ET');
    expect(receipts.querySelector('app-node-receipts-list')).not.toBeNull();
    expect(receipts.textContent).toContain('Reconciliation failed.');
    expect(receipts.textContent).toContain('Reconciliation State is Failed.');
    expect(receipts.textContent).toContain('Failure Reason is Broker snapshot disagrees with the intent WAL.');
    expect(receipts.textContent).toContain('Reconciliation Projection');
    expect(receipts.querySelector('[title*="Reconciliation Projection"]')).toBeTruthy();
    expect(receipts.textContent).toContain('Intent ID is intent-7.');
    expect(receipts.textContent).not.toContain('Intent 7');
    expect(receipts.textContent).not.toContain('timestamp unresolved');
  });

  it('keeps lifecycle node codes out of trader copy and formats them in receipts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    const hostState = status.lifecycle_chart.subgraphs['deploy'].nodes.find(
      (node) => node.id === 'host_state',
    );
    if (!hostState) throw new Error('Expected host-state lifecycle node in fixture.');
    hostState.summary = 'Host state requires one backend receipt before this run is ready.';
    hostState.evidence_summary = hostState.summary;
    hostState.receipts = [
      {
        label: 'host_process.disabled_reason_code',
        value: 'HOST_SERVICE_OFFLINE',
        headline: 'Host process is offline.',
        detail: 'The backend disabled reason is preserved in the audit payload.',
        unit: null,
        source: 'operator_surface.host_process',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
    ];
    const { fixture, component, element: el } = await setupBotControlPage({ status });
    component.selectLifecycleNode(hostState);
    fixture.detectChanges();
    el.querySelector<HTMLButtonElement>('[aria-label^="Open Deploy or start details"]')?.click();
    fixture.detectChanges();
    openLifecycleReceipts(fixture, el, 'deploy', 'host_state');

    const traderCopy = Array.from(el.querySelectorAll('[data-trader-copy]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    const receipts = Array.from(el.querySelectorAll('[data-receipt]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    expect(traderCopy).toContain('Host state requires one backend receipt');
    expect(traderCopy).not.toContain('HOST_SERVICE_OFFLINE');
    expect(receipts).toContain('Host Service Offline');
  });

  it('keeps Bot Control file-backed when the projection timeline is unavailable', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { element: el } = await setupBotControlPage({
      lifecycleTimelineFailure: new HttpErrorResponse({ status: 503 }),
    });
    expect(el.querySelector('app-overview-tab')).not.toBeNull();
    expect(el.querySelector('[data-testid="bot-control-recent-activity"]')?.textContent)
      .toContain('Projection unavailable; current snapshot remains file-backed.');
    expect(el.querySelector('.error-banner')?.textContent ?? '').not.toContain('Projection unavailable');
  });

  it('routes the trader guidance reconcile action to the existing instance endpoint', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, element, liveRuns } = await setupBotControlPage({
      mutationResponses: { reconcileInstance: makeReconcileAckResponse() },
    });

    const action = element.querySelector(
      '[data-testid="trader-guidance-primary-remediation"]',
    ) as HTMLButtonElement | null;
    expect(action?.textContent).toContain('Reconcile now');
    action?.click();
    await flush(fixture);

    expect(liveRuns.reconcileInstance).toHaveBeenCalledWith('sid-x');
  });

  it('does not render attention-row actions after folding attention into the ladder', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.trader_guidance.additional_attention_groups = [
      {
        code: 'reconciliation',
        severity: 'warning',
        headline: 'Reconciliation is not fresh-clean',
        explanation: 'Reconciliation state is NOT_AVAILABLE.',
        operator_next_step: 'Run reconciliation and wait for a clean or adopted receipt.',
        remediation: {
          kind: 'invoke_endpoint',
          endpoint: 'reconcile_instance',
          method: 'POST',
          path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
        },
      },
    ];
    const { element: el } = await setupBotControlPage({ status });

    expect(el.querySelector('[data-testid="bot-control-attention-toggle"]')).toBeNull();
    expect(el.querySelector('.attention-row-action')).toBeNull();
    expect(el.textContent).toContain('Reconciliation is not fresh-clean');
  });

  it('derives reconcile completion from refreshed backend status', async () => {
    const initial = makeStatus();
    const refreshed = makeStatus();
    refreshed.symbol = 'QQQ';
    const { fixture, component, element, liveRuns } = await setupBotControlPage({
      statusSequence: [initial, refreshed],
      mutationResponses: { reconcileInstance: makeReconcileAckResponse() },
    });

    await component.dispatchReconcileNow();
    await flush(fixture);
    fixture.detectChanges();

    expect(liveRuns.reconcileInstance).toHaveBeenCalledWith('sid-x');
    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(2);

    const text = element.textContent ?? '';
    expect(text).toContain('QQQ');
    expect(text).not.toContain('Reconcile succeeded');
    expect(text).not.toContain('Reconciled successfully');
  });

  it('renders backend reconcile precondition details instead of the generic load error', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reconcileError = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason_code: 'NO_LIVE_BINDING',
          message: 'No bot process is running for this instance - reconciliation requires a live engine.',
        },
      },
    });
    const { fixture, element } = await setupBotControlPage({
      mutationFailures: { reconcileInstance: reconcileError },
    });

    const action = element.querySelector(
      '[data-testid="trader-guidance-primary-remediation"]',
    ) as HTMLButtonElement | null;
    action?.click();
    await flush(fixture);
    fixture.detectChanges();

    const error = element.querySelector('.error-banner');
    expect(error?.textContent).toContain('No bot process is running for this instance');
    expect(error?.textContent).toContain('Start the instance before issuing commands');
    expect(error?.textContent).not.toContain('NO_LIVE_BINDING');
    expect(error?.textContent).not.toContain('Could not load bot control data');
  });

  it('derives destructive action completion from refreshed backend status', async () => {
    const initial = makeStatus({ markPoisonedEnabled: true });
    allowFlattenAndPause(initial);
    const refreshed = makeStatus({ markPoisonedEnabled: true });
    allowFlattenAndPause(refreshed);
    refreshed.symbol = 'QQQ';
    const { fixture, component, liveRuns, element } = await setupBotControlPage({
      statusSequence: [initial, refreshed],
      mutationResponses: {
        setInstanceDesiredState: makeDesiredStateResponse(),
        flattenAndPause: makeDesiredStateResponse(),
        issueInstanceCommand: makeCommandWriteResponse(),
      },
    });

    component.dispatchOverviewAction('stop');
    await flush(fixture);
    component.dispatchOverviewAction('flatten_and_pause');
    expect(component.flattenConfirmOpen()).toBe(true);
    await component.confirmFlatten();
    await flush(fixture);
    component.dispatchOverviewAction('mark_poisoned');
    expect(component.typedHaltOpen()).toBe(true);
    await component.confirmTypedHalt();
    await flush(fixture);

    expect(liveRuns.setInstanceDesiredState).toHaveBeenCalledWith('sid-x', {
      action: 'stop',
      reason: 'Stop',
      updated_by: 'operator',
    });
    expect(liveRuns.flattenAndPause).toHaveBeenCalledWith('sid-x', {
      action: 'pause',
      reason: 'Flatten and pause',
      updated_by: 'operator',
    });
    expect(liveRuns.issueInstanceCommand).toHaveBeenCalledWith('sid-x', { verb: 'MARK_POISONED' });
    expect(liveRuns.getInstanceStatus).toHaveBeenCalledTimes(4);

    const text = element.textContent ?? '';
    expect(text).toContain('QQQ');
    expect(text).not.toContain('Stop succeeded');
    expect(text).not.toContain('Flatten succeeded');
    expect(text).not.toContain('Marked poisoned successfully');
  });

  it('renders outcome-unknown destructive mutation results as operator copy', async () => {
    const stopError = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason_code: 'OUTCOME_UNKNOWN',
          message: 'Stop outcome is unknown. Reconcile before retrying.',
        },
      },
    });
    const { fixture, component, element } = await setupBotControlPage({
      mutationFailures: { setInstanceDesiredState: stopError },
    });

    component.dispatchOverviewAction('stop');
    await flush(fixture);

    const error = element.querySelector('.error-banner');
    expect(error?.textContent).toContain('Stop outcome is unknown');
    expect(error?.textContent).toContain('Resolve the blocker');
    expect(error?.textContent).not.toContain('OUTCOME_UNKNOWN');
    expect(error?.textContent).not.toContain('Stop succeeded');
  });

  it('re-derives selected lifecycle context from refreshed status data', async () => {
    const firstStatus = makeStatus();
    const secondStatus = makeStatus();
    const secondRecovery = secondStatus.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!secondRecovery) throw new Error('Expected recovery lifecycle node in fixture.');
    secondRecovery.status_label = 'Updated by poll';
    secondRecovery.evidence_summary = 'Recovery evidence refreshed.';
    const { fixture, component, element } = await setupBotControlPage({ status: firstStatus });

    const recovery = component.status()
      ?.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    component.selectLifecycleNode(recovery);
    fixture.detectChanges();

    component.status.set(secondStatus);
    fixture.detectChanges();

    const text = element.textContent ?? '';
    expect(text).toContain('Updated by poll');
    expect(text).toContain('Recovery evidence refreshed.');
  });

  it('requires typed HALT before marking a run poisoned', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, component, element: el, liveRuns } = await setupBotControlPage({
      status: makeStatus({ markPoisonedEnabled: true }),
      mutationResponses: { issueInstanceCommand: makeCommandWriteResponse() },
    });
    component.dispatchOverviewAction('mark_poisoned');
    fixture.detectChanges();

    const submit = el.querySelector('[data-testid="typed-halt-confirm-submit"]') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    const input = el.querySelector('[data-testid="typed-halt-confirm-input"]') as HTMLInputElement;
    input.value = 'HALT';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    submit.click();
    await flush(fixture);

    expect(liveRuns.issueInstanceCommand).toHaveBeenCalledWith('sid-x', { verb: 'MARK_POISONED' });
  });

  it('confirms Flatten & pause through a dialog before calling the mutation', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    allowFlattenAndPause(status);
    const { fixture, component, liveRuns } = await setupBotControlPage({
      status,
      mutationResponses: { flattenAndPause: makeDesiredStateResponse() },
    });

    // Dispatching flatten opens the confirm dialog and does not call the mutation yet.
    component.dispatchOverviewAction('flatten_and_pause');
    fixture.detectChanges();
    expect(component.flattenConfirmOpen()).toBe(true);
    expect(liveRuns.flattenAndPause).not.toHaveBeenCalled();

    await component.confirmFlatten();
    expect(liveRuns.flattenAndPause).toHaveBeenCalledWith('sid-x', {
      action: 'pause',
      reason: 'Flatten and pause',
      updated_by: 'operator',
    });
  });

  it('dismisses the control-plane banner for the session', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, component, element: el } = await setupBotControlPage();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).not.toBeNull();

    component.dismissControlPlaneBanner();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).toBeNull();
  });

  it('does not flatten when the action became disabled while the confirm dialog was open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    allowFlattenAndPause(status);
    const { fixture, component, liveRuns } = await setupBotControlPage({ status });

    component.dispatchOverviewAction('flatten_and_pause');
    fixture.detectChanges();
    expect(component.flattenConfirmOpen()).toBe(true);

    // A poll disables the action while the dialog is still open.
    const disabled = makeStatus();
    disabled.operator_surface.actions.flatten_and_pause = {
      enabled: false,
      effect: 'LIVE_ACTUATION',
      disabled_reason_code: 'NO_OWNED_POSITIONS',
      disabled_reasons: ['NO_OWNED_POSITIONS'],
      gate_results: [],
    };
    component.status.set(disabled);

    await component.confirmFlatten();
    expect(liveRuns.flattenAndPause).not.toHaveBeenCalled();
  });

  it('re-shows a dismissed control-plane banner when its rendered content changes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, component, element: el } = await setupBotControlPage();
    component.dismissControlPlaneBanner();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).toBeNull();

    // Same UNREACHABLE state but materially different notice content -> reappears.
    const next = makeStatus();
    const cp = next.operator_surface.control_plane;
    if (cp) cp.notice = 'A newer, different command-channel failure notice.';
    component.status.set(next);
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).not.toBeNull();
  });

  it('re-shows a dismissed control-plane banner after recovery and a later re-failure', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { fixture, component, element: el } = await setupBotControlPage();
    component.dismissControlPlaneBanner();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).toBeNull();

    // Recover: control plane CONNECTED -> banner clears and the dismissal resets.
    const recovered = makeStatus();
    const recoveredCp = recovered.operator_surface.control_plane;
    if (recoveredCp) recoveredCp.state = 'CONNECTED';
    component.status.set(recovered);
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).toBeNull();

    // Fail back to the same UNREACHABLE state -> new outage, banner reappears.
    component.status.set(makeStatus());
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')).not.toBeNull();
  });

  it('updates inline attention content when a new critical group arrives', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.trader_guidance.additional_attention_groups = [
      {
        code: 'broker_safety',
        severity: 'critical',
        headline: 'Broker safety is unsafe',
        explanation: 'Paper-safety evidence is unsafe.',
        operator_next_step: 'Inspect broker/account safety evidence before any trading action.',
        remediation: { kind: 'open_runbook', slug: 'broker-instance-operator-surface' },
      },
    ];
    const { fixture, component, element: el } = await setupBotControlPage({ status });

    expect(el.querySelector('[data-testid="bot-control-attention-panel"]')).toBeNull();
    expect(el.textContent).toContain('Broker safety is unsafe');

    const next = makeStatus();
    next.operator_surface.trader_guidance.additional_attention_groups = [
      {
        code: 'reconciliation',
        severity: 'critical',
        headline: 'Reconciliation failed',
        explanation: 'The cold-start reconciliation receipt failed.',
        operator_next_step: 'Run reconciliation and wait for a clean or adopted receipt.',
        remediation: {
          kind: 'invoke_endpoint',
          endpoint: 'reconcile_instance',
          method: 'POST',
          path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
        },
      },
    ];
    component.status.set(next);
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="bot-control-attention-panel"]')).toBeNull();
    expect(el.textContent).toContain('Reconciliation failed');
  });
});
