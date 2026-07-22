import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BotRollCallResponse,
  BotDeleteResponse,
  CrashRecoveryOverrideResponse,
  LiveInstanceStatus,
} from '../../../api/live-instances.types';
import {
  makeBotLifecycleMutationResponse,
  makeDesiredStateResponse,
  makeHostRunnerHealth,
  makeRuntimeFreshnessWithLeaseAction,
  makeStatus,
} from './bot-control-page.fixtures';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import {
  deferred,
  flush,
  installBotControlPageTestStubs,
  setupBotControlPage,
} from './bot-control-page.testing';

function startableReadyStatus(): LiveInstanceStatus {
  return makeStatus({
    startCapabilityEnabled: true,
    startRunId: 'run-x',
  });
}

function offDutyNeedsRollCallStatus(): LiveInstanceStatus {
  const status = makeStatus({
    startCapabilityEnabled: true,
    startRunId: 'run-x',
  });
  status.daily_lifecycle.display_status = 'Off duty';
  status.daily_lifecycle.attention_badge = null;
  status.daily_lifecycle.reason = 'Run roll call to issue a start offer.';
  status.daily_lifecycle.primary_action = null;
  return status;
}

function onDutyStatus(): LiveInstanceStatus {
  const status = makeStatus({ hostState: 'RUNNING' });
  status.daily_lifecycle.display_status = 'On duty';
  status.daily_lifecycle.reason = 'The bot is running.';
  return status;
}

function unreachableSickBayStatus(): LiveInstanceStatus {
  const status = makeStatus({ hostState: 'UNREACHABLE' });
  status.daily_lifecycle.display_status = 'Sick bay';
  status.daily_lifecycle.reason = 'The host process cannot be confirmed.';
  return status;
}

function startableStatusWithoutOffer(): LiveInstanceStatus {
  const status = startableReadyStatus();
  if (!status.daily_lifecycle.primary_action) {
    throw new Error('Fixture expected a primary start action.');
  }
  status.daily_lifecycle.primary_action = {
    ...status.daily_lifecycle.primary_action,
    offer_id: null,
    expires_at_ms: null,
  };
  return status;
}

function observationLeaseBlockedStatus(): LiveInstanceStatus {
  const status = startableReadyStatus();
  status.start_defaults = {
    strategy: 'deployment_validation',
    readonly: false,
    hydrate_policy: 'require',
    max_orders_per_day: 2,
    ibkr_host: '127.0.0.1',
    account_id: 'DU1234567',
  };
  status.operator_surface.host_process.start_capability = {
    enabled: false,
    run_id: null,
    request: null,
    disabled_reason_code: 'ACCOUNT_EVIDENCE_STALE',
    gate_results: [
      {
        gate_id: 'account.observation_lease',
        status: 'block',
        source: 'account_observation_lease',
        operator_reason: 'Account verification is overdue.',
        operator_next_step: 'RECONCILE_NOW',
        evidence_at_ms: 1_700_000_000_000,
      },
    ],
  };
  return status;
}

function observationLeaseReadyStatus(): LiveInstanceStatus {
  const status = startableReadyStatus();
  status.operator_surface.host_process.start_capability.gate_results.unshift({
    gate_id: 'account.observation_lease',
    status: 'pass',
    source: 'account_observation_lease',
    operator_reason: 'Account verified.',
    operator_next_step: null,
    evidence_at_ms: 1_700_000_000_000,
  });
  return status;
}

function rollCallResponse(): BotRollCallResponse {
  return {
    summary: {
      ready: 1,
      off_roster: 0,
      sick_bay: 0,
      on_duty: 0,
      off_duty: 0,
      retired: 0,
      generated_at_ms: 1_700_000_000_000,
      session_date: '2026-07-13',
      effective_stop_ms: 1_700_050_000_000,
    },
    offers: [
      {
        offer_id: 'offer-from-roll-call',
        strategy_instance_id: 'sid-x',
        run_id: 'run-x',
        session_date: '2026-07-13',
        issued_at_ms: 1_700_000_000_000,
        expires_at_ms: 1_700_000_600_000,
      },
    ],
  };
}

function crashRecoveryStatus(): LiveInstanceStatus {
  const status = makeStatus();
  status.operator_surface.host_process.start_capability = {
    enabled: false,
    run_id: null,
    request: null,
    disabled_reason_code: 'CRASH_RECOVERY_REQUIRED',
    gate_results: [],
  };
  return status;
}

function crashRecoveryResponse(): CrashRecoveryOverrideResponse {
  return {
    accepted: true,
    account_id: 'DU1',
    strategy_instance_id: 'sid-x',
    run_id: 'run-x',
    bot_order_namespace: 'learn-ai/sid-x/v1',
    override_id: 'override-1',
    recorded_at_ms: 0,
    blocking_recorded_at_ms: 0,
    event_type: 'account_audited_override_recorded',
    rung_receipt: null,
    rung_receipt_warnings: [],
  };
}

function removeBotResponse(): BotDeleteResponse {
  return {
    strategy_instance_id: 'sid-x',
    mode: 'soft',
    deleted_at_ms: 0,
    deleted_by: 'operator',
    reason: null,
    deleted_run_ids: [],
    marker_path: '/tmp/sid-x.deleted',
    hidden_from_catalog: true,
  };
}

function terminalRemoveStatus(): LiveInstanceStatus {
  const status = makeStatus();
  status.operator_surface.blockers = [
    operatorBlockerFixture({
      id: 'retired',
      scope: 'bot',
      disposition: 'terminal',
      headline: "Can't recover",
      detail: 'This bot has been retired. Remove it from the catalog or replace it.',
      primaryMove: {
        label: 'Remove',
        action: { kind: 'remove' },
        target: null,
        confirmation: {
          title: 'Move-authored remove title',
          body: 'Move-authored remove body.',
          consequence: 'Move-authored remove consequence.',
          confirm_label: 'Remove bot from move',
          required_token: '',
        },
      },
      secondaryMoves: [],
      appliesTo: 'run',
    }),
  ];
  return status;
}

function terminalReplaceStatus(): LiveInstanceStatus {
  const status = makeStatus();
  status.daily_lifecycle.primary_action = null;
  status.operator_surface.blockers = [
    operatorBlockerFixture({
      id: 'run_poisoned',
      scope: 'bot',
      disposition: 'terminal',
      headline: "Can't recover",
      detail: 'This run is poisoned and cannot be restarted safely.',
      primaryMove: {
        label: 'Replace',
        action: { kind: 'retire_replace' },
        target: null,
        confirmation: {
          title: 'Move-authored replace title',
          body: 'Move-authored replace body.',
          consequence: 'Move-authored replace consequence.',
          confirm_label: 'Replace from move',
          required_token: '',
        },
      },
      secondaryMoves: [],
      appliesTo: 'run',
    }),
  ];
  return status;
}

describe('BotControlPageComponent', () => {
  beforeEach(() => {
    installBotControlPageTestStubs();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it('opens in the trader lens and preserves the operator cockpit behind the lens switch', async () => {
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status: startableReadyStatus(),
    });
    await flush(fixture);

    expect(element.querySelector('app-trader-view')).not.toBeNull();
    expect(element.querySelector('app-verdict-card')).toBeNull();
    expect(element.querySelectorAll('app-overview-tab')).toHaveLength(0);
    expect(element.textContent).toContain('This bot is ready');
    expect(element.textContent).toContain('Market hours unavailable');
    expect(liveRuns.getLifecycleTimeline).not.toHaveBeenCalled();

    const operations = Array.from(element.querySelectorAll<HTMLButtonElement>('.bot-lens-switch button'))
      .find((button) => button.textContent?.trim() === 'Operations');
    operations?.click();
    fixture.detectChanges();

    expect(element.querySelector('app-verdict-card')).not.toBeNull();
    expect(element.querySelectorAll('app-overview-tab')).toHaveLength(1);
    expect(element.querySelector('app-trader-guidance-pane')).not.toBeNull();
    expect(element.querySelector('app-bot-control-side-panel')).not.toBeNull();
    expect(element.querySelector('#verdict-state')?.textContent).toContain('Ready');
    expect(element.querySelector('app-trader-guidance-pane')?.textContent).toContain('Proof stack');
    // Deleted surfaces must not return.
    expect(element.querySelector('[data-testid="bot-control-workbench-tabs"]')).toBeNull();
    expect(element.querySelector('.posture-pills')).toBeNull();
    expect(element.querySelector('app-node-inspector')).toBeNull();
    expect(element.querySelector('app-trader-guidance-timeline')).toBeNull();
  });

  it('dispatches the start-host-runner mutation when the primary verb is clicked', async () => {
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status: startableReadyStatus(),
      mutationResponses: {
        startHostRunner: { accepted: true, process: makeHostRunnerHealth().process },
      },
    });

    const verb = element.querySelector<HTMLButtonElement>('[data-testid="trader-primary-action"]');
    expect(verb?.textContent?.trim()).toBe('Start');

    verb?.click();
    await flush(fixture);

    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-x', {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
      roll_call_offer_id: 'offer-run-x',
    });
  });

  it('guides an on-duty bot through graceful stop and records durable STOPPED intent', async () => {
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status: onDutyStatus(),
      mutationResponses: { setInstanceDesiredState: makeDesiredStateResponse() },
    });

    expect(element.textContent).toContain('End this bot safely');
    expect(element.textContent).toContain('does not submit orders or flatten the account');

    const stop = element.querySelector<HTMLButtonElement>('[data-testid="trader-graceful-stop"]');
    expect(stop?.disabled).toBe(false);
    stop?.click();
    await flush(fixture);

    expect(liveRuns.setInstanceDesiredState).toHaveBeenCalledWith('sid-x', {
      action: 'stop',
      reason: 'Stop',
      updated_by: 'operator',
    });
  });

  it('keeps graceful stop available when a sick-bay bot has unproven host liveness', async () => {
    const { element } = await setupBotControlPage({
      status: unreachableSickBayStatus(),
    });

    expect(element.textContent).toContain('No live process can be confirmed.');
    expect(element.textContent).toContain('blocks a future start');
    expect(element.querySelector('[data-testid="trader-graceful-stop"]')).not.toBeNull();
  });

  it('prepares a roll-call start offer in the bot cockpit when the bot is off duty', async () => {
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status: offDutyNeedsRollCallStatus(),
      mutationResponses: { runRollCall: rollCallResponse() },
    });
    await flush(fixture);

    expect(liveRuns.runRollCall).toHaveBeenCalledTimes(1);
    expect(liveRuns.startHostRunner).not.toHaveBeenCalled();
    expect(element.textContent).toContain('Start offer ready');
    expect(element.textContent).toContain('waiting for the cockpit snapshot to show Start');
  });

  it('runs roll call and starts with the returned offer when start is requested without one', async () => {
    const { fixture, component, liveRuns } = await setupBotControlPage({
      status: startableStatusWithoutOffer(),
      mutationResponses: {
        runRollCall: rollCallResponse(),
        startHostRunner: { accepted: true, process: makeHostRunnerHealth().process },
      },
    });

    await component.dispatchStartProcess();
    await flush(fixture);

    expect(liveRuns.runRollCall).toHaveBeenCalledTimes(1);
    expect(liveRuns.startHostRunner).toHaveBeenCalledWith('run-x', {
      readonly: false,
      hydrate_policy: 'require',
      strategy: 'deployment_validation',
      max_orders_per_day: 2,
      ibkr_host: '127.0.0.1',
      roll_call_offer_id: 'offer-from-roll-call',
    });
  });

  it('shows account verification while the start boundary is being checked', async () => {
    const startResponse = {
      accepted: true as const,
      process: makeHostRunnerHealth().process,
    };
    const pendingStart = deferred<typeof startResponse>();
    const { fixture, component, element } = await setupBotControlPage({
      status: observationLeaseReadyStatus(),
      mutationResponses: { startHostRunner: pendingStart.promise },
    });

    const start = component.dispatchStartProcess();
    fixture.detectChanges();

    expect(element.textContent).toContain('Verifying account');
    expect(element.textContent).toContain('Account verified.');

    pendingStart.resolve(startResponse);
    await start;
    await flush(fixture);
    expect(element.textContent).toContain('Startup request sent');
  });

  it('renders one account remedy and reconciles the bound account when observation blocks Start', async () => {
    const { fixture, element, broker, liveRuns } = await setupBotControlPage({
      status: observationLeaseBlockedStatus(),
    });
    broker.reconcileAccount.mockResolvedValue({});

    expect(element.textContent).toContain('Account verification blocked');
    expect(element.textContent).toContain('Account verification is overdue.');
    const buttons = Array.from(
      element.querySelectorAll<HTMLButtonElement>('.startup-automation button'),
    );
    expect(buttons).toHaveLength(1);
    expect(buttons[0]?.textContent?.trim()).toBe('Reconcile now');

    buttons[0]?.click();
    await flush(fixture);

    expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    expect(liveRuns.reconcileInstance).not.toHaveBeenCalled();
    expect(element.textContent).toContain('Account verification is overdue.');
  });

  it('surfaces the crash-recovery verb and records the override after confirmation', async () => {
    const { fixture, component, element, liveRuns } = await setupBotControlPage({
      status: crashRecoveryStatus(),
      mutationResponses: { recordCrashRecoveryOverride: crashRecoveryResponse() },
    });

    const verb = element.querySelector<HTMLButtonElement>('[data-testid="trader-primary-action"]');
    expect(verb?.textContent?.trim()).toBe('Record recovery evidence');

    verb?.click();
    await flush(fixture);
    expect(component.crashRecoveryConfirmOpen()).toBe(true);

    await component.confirmCrashRecoveryOverride();

    expect(liveRuns.recordCrashRecoveryOverride).toHaveBeenCalledWith('sid-x', {
      confirm_account_flat: true,
      approved_by: 'operator',
    });
  });

  it('confirms before removing a terminal bot', async () => {
    const { fixture, component, element, liveRuns } = await setupBotControlPage({
      status: terminalRemoveStatus(),
      mutationResponses: { deleteBot: removeBotResponse() },
    });

    const remove = element.querySelector<HTMLButtonElement>('.trader-terminal-action');
    expect(remove?.textContent?.trim()).toBe('Remove');

    remove?.click();
    await flush(fixture);

    expect(component.removeBotConfirmOpen()).toBe(true);
    expect(element.textContent).toContain('Move-authored remove title');
    expect(element.textContent).toContain('Move-authored remove consequence.');
    expect(element.textContent).toContain('Remove bot from move');
    expect(liveRuns.deleteBot).not.toHaveBeenCalled();

    await component.confirmRemoveBot();

    expect(liveRuns.deleteBot).toHaveBeenCalledWith('sid-x', {
      mode: 'soft',
      deleted_by: 'operator',
    });
  });

  it('opens Retire & Replace from a terminal move even without a lifecycle action', async () => {
    const { fixture, component, element, liveRuns } = await setupBotControlPage({
      status: terminalReplaceStatus(),
      mutationResponses: { botLifecycleMutation: makeBotLifecycleMutationResponse() },
    });

    const replace = element.querySelector<HTMLButtonElement>('.trader-terminal-action');
    expect(replace?.textContent?.trim()).toBe('Replace');

    replace?.click();
    await flush(fixture);

    expect(component.retireReplaceConfirmOpen()).toBe(true);
    expect(element.textContent).toContain('Move-authored replace title');
    expect(element.textContent).toContain('Move-authored replace consequence.');
    expect(element.textContent).toContain('Replace from move');

    await component.confirmRetireReplace();

    expect(liveRuns.retireAndReplace).toHaveBeenCalledWith('sid-x', {
      confirm_account_flat: true,
      replacement_requested: true,
      updated_by: 'operator',
      reason: 'Retire & Replace',
    });
  });

  it('shows an error banner when the status request fails', async () => {
    const { element } = await setupBotControlPage({
      surfaceError: 'status boom',
    });

    const banner = element.querySelector('.error-banner');
    expect(banner?.textContent).toContain('status boom');
    expect(element.querySelector('app-trader-view')).toBeNull();
    expect(element.querySelector('app-verdict-card')).toBeNull();
  });

  it('keeps the stream snapshot read-only and renders each backend freshness age', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_700_000_000_000);
    const status = startableReadyStatus();
    status.fetched_at_ms = Date.now();
    status.operator_surface.runtime_freshness = makeRuntimeFreshnessWithLeaseAction();
    const { fixture, element, surface } = await setupBotControlPage({ status });

    surface.readOnly.set(true);
    fixture.detectChanges();

    const banner = element.querySelector('.stale-banner');
    const copy = banner?.textContent?.replace(/\s+/g, ' ');
    expect(copy).toContain('Command loop: FRESH · 100 ms old');
    expect(copy).toContain('Runtime control plane: STALE · 30000 ms old');
    expect(element.querySelector<HTMLButtonElement>('[data-testid="trader-primary-action"]')?.disabled)
      .toBe(true);
  });
});
