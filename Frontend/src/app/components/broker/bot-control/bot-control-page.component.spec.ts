import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  CrashRecoveryOverrideResponse,
  LiveInstanceStatus,
} from '../../../api/live-instances.types';
import { makeHostRunnerHealth, makeStatus } from './bot-control-page.fixtures';
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

describe('BotControlPageComponent', () => {
  beforeEach(() => {
    installBotControlPageTestStubs();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it('renders the Verdict Card and none of the deleted surfaces', async () => {
    const { element } = await setupBotControlPage({ status: startableReadyStatus() });

    expect(element.querySelector('app-verdict-card')).not.toBeNull();
    expect(element.querySelector('#verdict-state')?.textContent).toContain('Ready');
    // Deleted surfaces must not return.
    expect(element.querySelector('app-overview-tab')).toBeNull();
    expect(element.querySelector('[data-testid="bot-control-workbench-tabs"]')).toBeNull();
    expect(element.querySelector('.posture-pills')).toBeNull();
    expect(element.querySelector('app-bot-control-side-panel')).toBeNull();
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

  it('shows an error banner when the status request fails', async () => {
    const { element } = await setupBotControlPage({
      configureLiveRuns: (liveRuns) => {
        liveRuns.getInstanceStatus.mockRejectedValue(new Error('status boom'));
      },
    });

    const banner = element.querySelector('.error-banner');
    expect(banner?.textContent).toContain('status boom');
    expect(element.querySelector('app-verdict-card')).toBeNull();
  });
});
