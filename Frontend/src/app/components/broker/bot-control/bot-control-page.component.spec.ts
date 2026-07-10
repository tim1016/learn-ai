import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BotDeleteResponse,
  CrashRecoveryOverrideResponse,
  LiveInstanceStatus,
} from '../../../api/live-instances.types';
import {
  makeBotLifecycleMutationResponse,
  makeHostRunnerHealth,
  makeRuntimeFreshnessWithLeaseAction,
  makeStatus,
} from './bot-control-page.fixtures';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import {
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

  it('renders stream-primary cockpit surfaces without the deleted timeline fetch', async () => {
    const { fixture, element, liveRuns } = await setupBotControlPage({
      status: startableReadyStatus(),
    });
    await flush(fixture);

    expect(element.querySelector('app-verdict-card')).not.toBeNull();
    expect(element.querySelector('app-overview-tab')).not.toBeNull();
    expect(element.querySelector('app-trader-guidance-pane')).not.toBeNull();
    expect(element.querySelector('app-bot-control-side-panel')).not.toBeNull();
    expect(element.querySelector('#verdict-state')?.textContent).toContain('Ready');
    expect(liveRuns.getLifecycleTimeline).not.toHaveBeenCalled();
    expect(element.querySelector('app-trader-guidance-pane')?.textContent)
      .toContain('Proof stack');
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

    const verb = element.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]');
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

  it('surfaces the crash-recovery verb and records the override after confirmation', async () => {
    const { fixture, component, element, liveRuns } = await setupBotControlPage({
      status: crashRecoveryStatus(),
      mutationResponses: { recordCrashRecoveryOverride: crashRecoveryResponse() },
    });

    const verb = element.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]');
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

    const remove = element.querySelector<HTMLButtonElement>('.vc-terminal-action');
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

    const replace = element.querySelector<HTMLButtonElement>('.vc-terminal-action');
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
    expect(element.querySelector<HTMLButtonElement>('[data-testid="verdict-verb"]')?.disabled)
      .toBe(true);
  });
});
