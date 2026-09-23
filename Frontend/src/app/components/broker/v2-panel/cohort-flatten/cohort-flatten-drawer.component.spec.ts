import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  CohortActionResult,
  CohortFlattenCohort,
  CohortFlattenLeg,
  CohortFlattenView,
  CohortLegResult,
} from '../lib/broker-v2-panel.types';
import { CohortFlattenDrawerComponent } from './cohort-flatten-drawer.component';
import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';

function leg(overrides: Partial<CohortFlattenLeg> = {}): CohortFlattenLeg {
  return {
    strategy_instance_id: 'qqq-1',
    action_id: 'execute_safe_flatten',
    enabled: true,
    revision: 3,
    concurrency_token: 'tok-1',
    blocker_headline: null,
    exposure: { QQQ: 10 },
    ...overrides,
  };
}

function cohort(
  legs: CohortFlattenLeg[],
  overrides: Partial<CohortFlattenCohort> = {},
): CohortFlattenCohort {
  return {
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    symbol: 'QQQ',
    legs,
    enabled_count: legs.filter((item) => item.enabled).length,
    ...overrides,
  };
}

function view(cohorts: CohortFlattenCohort[]): CohortFlattenView {
  return { account_id: 'PA1', observed_at_ms: 1_700_000_000_000, cohorts };
}

const QQQ_COHORT = cohort([
  leg(),
  leg({ strategy_instance_id: 'qqq-2', revision: 5, concurrency_token: 'tok-2', exposure: { QQQ: -4 } }),
  leg({
    strategy_instance_id: 'qqq-3',
    enabled: false,
    revision: null,
    concurrency_token: null,
    blocker_headline: 'This bot is still running. Stop it first.',
    exposure: { QQQ: 2 },
  }),
]);

const SPY_COHORT = cohort(
  [
    leg({ strategy_instance_id: 'spy-1', concurrency_token: 'tok-s1', exposure: { SPY: 1 } }),
    leg({ strategy_instance_id: 'spy-2', concurrency_token: 'tok-s2', exposure: { SPY: 1 } }),
  ],
  { symbol: 'SPY' },
);

function applied(sid: string, receipt = `receipt-${sid}`): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome: 'applied',
    result: {
      action_id: 'execute_safe_flatten',
      outcome: 'success',
      receipt_id: receipt,
      recorded_at_ms: 1_700_000_000_500,
      applied: true,
      revision: 4,
      concurrency_token: 'next',
      message: `Flatten submitted for ${sid}.`,
    },
    error: null,
  };
}

function refused(
  sid: string,
  overrides: Partial<NonNullable<CohortLegResult['error']>> = {},
  outcome: CohortLegResult['outcome'] = 'refused',
): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome,
    result: null,
    error: {
      action_id: 'execute_safe_flatten',
      outcome: outcome === 'refused' ? 'conflict' : outcome === 'unknown' ? 'unknown' : 'failure',
      receipt_id: null,
      recorded_at_ms: 1,
      message: 'This action changed since it was presented.',
      why: 'The bot resumed after the roster was read.',
      reason_code: 'STALE_REVISION',
      ...overrides,
    },
  };
}

function result(legs: CohortLegResult[]): CohortActionResult {
  const count = (...kinds: string[]) => legs.filter((item) => kinds.includes(item.outcome)).length;
  return {
    account_id: 'PA1',
    receipt_id: 'batch',
    recorded_at_ms: 1_700_000_001_000,
    legs,
    applied_count: count('applied'),
    replayed_count: count('replayed'),
    refused_count: count('refused'),
    failed_count: count('failed', 'unknown'),
  };
}

function fakeService(cohorts: CohortFlattenCohort[], batch = result([applied('qqq-1'), applied('qqq-2')])) {
  return {
    getCohortFlattenView: vi.fn().mockResolvedValue(view(cohorts)),
    runCohortFlatten: vi.fn().mockResolvedValue(batch),
  };
}

function open(
  service: ReturnType<typeof fakeService>,
  overrides: { directory?: ReturnType<typeof provideFleetDirectory> } = {},
) {
  const directory =
    overrides.directory ??
    provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
  return render(CohortFlattenDrawerComponent, {
    inputs: { clerkId: 'clrk_spec', visible: true, broker: 'alpaca', accountId: 'PA1' },
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
}

function checkbox(sid: string): HTMLInputElement {
  return screen.getByRole('checkbox', { name: sid }) as HTMLInputElement;
}

async function confirmWave(user: ReturnType<typeof userEvent.setup>, count: number): Promise<void> {
  await user.click(screen.getByRole('button', { name: `Review flatten of ${count}` }));
  const dialog = await screen.findByRole('dialog');
  await user.type(within(dialog).getByRole('textbox'), 'FLATTEN');
  await user.click(within(dialog).getByRole('button', { name: `Flatten ${count}` }));
}

afterEach(() => vi.restoreAllMocks());

describe('CohortFlattenDrawerComponent', () => {
  it('renders each cohort with its legs, attributed exposure, and the blocker of a disabled leg', async () => {
    await open(fakeService([QQQ_COHORT]));

    expect(await screen.findByText('Deployment Validation')).toBeTruthy();
    expect(screen.getByText('QQQ 10')).toBeTruthy();
    expect(screen.getByText('QQQ -4')).toBeTruthy();
    expect(screen.getByText('This bot is still running. Stop it first.')).toBeTruthy();
    expect(checkbox('qqq-3').disabled).toBe(true);
  });

  it('says the read failed, never that there are no cohorts, when the GET fails', async () => {
    const service = fakeService([]);
    service.getCohortFlattenView = vi.fn().mockRejectedValue(new Error('503'));

    await open(service);

    expect((await screen.findByRole('alert')).textContent).toContain('Could not read');
    expect(screen.queryByText(/No cohort on this account/)).toBeNull();
  });

  it('says so plainly when the account has no multi-bot cohort', async () => {
    await open(fakeService([]));

    expect(await screen.findByText(/No cohort on this account/)).toBeTruthy();
  });

  it('defaults the selection to the armed legs of one cohort', async () => {
    await open(fakeService([QQQ_COHORT, SPY_COHORT]));

    await screen.findAllByRole('checkbox');
    expect(checkbox('qqq-1').checked).toBe(true);
    expect(checkbox('qqq-2').checked).toBe(true);
    expect(checkbox('qqq-3').checked).toBe(false);
    // Another cohort's legs are never swept into this wave.
    expect(checkbox('spy-1').checked).toBe(false);
    expect(checkbox('spy-1').disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Review flatten of 2' })).toBeTruthy();
  });

  it('switches the wave to another cohort only through that cohort’s own button', async () => {
    await open(fakeService([QQQ_COHORT, SPY_COHORT]));
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: /Flatten this cohort: Deployment Validation SPY/ }));

    expect(checkbox('spy-1').checked).toBe(true);
    expect(checkbox('spy-2').checked).toBe(true);
    expect(checkbox('qqq-1').checked).toBe(false);
  });

  it('arms a cohort’s button only when the backend armed one of its legs', async () => {
    const blocked = cohort(
      [
        leg({ strategy_instance_id: 'iwm-1', enabled: false, revision: null, concurrency_token: null, blocker_headline: 'Running.' }),
        leg({ strategy_instance_id: 'iwm-2', enabled: false, revision: null, concurrency_token: null, blocker_headline: 'Running.' }),
      ],
      { symbol: 'IWM' },
    );
    await open(fakeService([QQQ_COHORT, blocked]));
    await screen.findAllByRole('checkbox');

    const button = screen.getByRole('button', {
      name: /Flatten this cohort: Deployment Validation IWM/,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it('shows the blast radius and requires the typed token before anything is sent', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: 'Review flatten of 2' }));
    const dialog = await screen.findByRole('dialog');

    expect(dialog.textContent).toContain('2 bots');
    expect(dialog.textContent).toContain('account PA1');
    expect(dialog.textContent).toContain('qqq-1 QQQ 10');
    expect(dialog.textContent).toContain('qqq-2 QQQ -4');
    expect(dialog.textContent).not.toContain('qqq-3');
    const commit = within(dialog).getByRole('button', { name: 'Flatten 2' }) as HTMLButtonElement;
    expect(commit.disabled).toBe(true);
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  it('returns focus to the review control when the confirmation is cancelled', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: 'Review flatten of 2' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Review flatten of 2' })),
    );
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  it('sends exactly the confirmed legs, each with the identity it was presented with', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(checkbox('qqq-1'));
    await confirmWave(user, 1);

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(1);
    const [target, request] = service.runCohortFlatten.mock.calls[0];
    expect(target).toMatchObject({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA1' });
    expect(target.idempotencyKey).toBe(request.idempotency_key);
    expect(request.legs).toEqual([
      {
        strategy_instance_id: 'qqq-2',
        action_id: 'execute_safe_flatten',
        revision: 5,
        concurrency_token: 'tok-2',
      },
    ]);
  });

  it('renders every leg outcome in request order: receipts, and typed refusals through the label pipe', async () => {
    const service = fakeService(
      [QQQ_COHORT],
      result([applied('qqq-1', 'rcpt/0001'), refused('qqq-2')]),
    );
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    const outcome = await screen.findByRole('region', { name: 'Flatten outcome' });
    const items = within(outcome).getAllByRole('listitem');
    expect(items.map((item) => item.querySelector('code')?.textContent)).toEqual(['qqq-1', 'qqq-2']);
    expect(within(items[0]).getByText('rcpt/0001')).toBeTruthy();
    expect(within(items[0]).getByText('Flatten submitted for qqq-1.')).toBeTruthy();
    expect(within(items[1]).getByText(formatReceiptLabel('STALE_REVISION'))).toBeTruthy();
    expect(within(items[1]).queryByText('STALE_REVISION')).toBeNull();
    expect(within(items[1]).getByText('This action changed since it was presented.')).toBeTruthy();
    expect(within(items[1]).getByText('The bot resumed after the roster was read.')).toBeTruthy();
  });

  it('renders an account-scoped early exit as the account blocker, keeping attempted outcomes', async () => {
    const three = cohort([
      leg(),
      leg({ strategy_instance_id: 'qqq-2', concurrency_token: 'tok-2' }),
      leg({ strategy_instance_id: 'qqq-4', concurrency_token: 'tok-4' }),
    ]);
    const service = fakeService(
      [three],
      result([
        applied('qqq-1'),
        refused(
          'qqq-2',
          {
            message: 'This account’s execution lease is lost.',
            why: 'Another process owns this account.',
            reason_code: 'EXECUTION_LEASE_LOST',
          },
          'failed',
        ),
      ]),
    );
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 3);

    const blocker = await screen.findByRole('alert', { name: 'Batch stopped: account-scoped' });
    expect(blocker.textContent).toContain('after 2 of 3 bots');
    expect(within(blocker).getByText(formatReceiptLabel('EXECUTION_LEASE_LOST'))).toBeTruthy();
    expect(within(blocker).getByText('Another process owns this account.')).toBeTruthy();
    expect(within(blocker).getByText('qqq-4')).toBeTruthy();
    const outcome = screen.getByRole('region', { name: 'Flatten outcome' });
    expect(within(outcome).getByText('receipt-qqq-1')).toBeTruthy();
  });

  it('retries the same wave under the same durable key and the same legs', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), refused('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);
    await user.click(await screen.findByRole('button', { name: 'Retry this batch' }));

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(2);
    const [firstTarget, firstRequest] = service.runCohortFlatten.mock.calls[0];
    const [retryTarget, retryRequest] = service.runCohortFlatten.mock.calls[1];
    expect(retryTarget).toBe(firstTarget);
    expect(retryRequest).toBe(firstRequest);
    expect(retryRequest.idempotency_key).toBe(firstRequest.idempotency_key);
  });

  it('offers no retry once every leg applied or replayed', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    await screen.findByRole('region', { name: 'Flatten outcome' });
    expect(screen.queryByRole('button', { name: 'Retry this batch' })).toBeNull();
  });

  it('reports an unknown outcome when the POST never lands, and retries under the same key', async () => {
    const service = fakeService([QQQ_COHORT]);
    service.runCohortFlatten = vi
      .fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(result([applied('qqq-1'), applied('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    expect((await screen.findByRole('alert')).textContent).toContain('did not reach a result');
    await user.click(screen.getByRole('button', { name: 'Retry this batch' }));
    const [, first] = service.runCohortFlatten.mock.calls[0];
    const [, retry] = service.runCohortFlatten.mock.calls[1];
    expect(retry.idempotency_key).toBe(first.idempotency_key);
  });

  it('mints a new key for a genuinely new wave from the re-read presentation', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), refused('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);
    await screen.findByRole('region', { name: 'Flatten outcome' });
    // The drawer re-read the presentation after the batch.
    await vi.waitFor(() => expect(service.getCohortFlattenView).toHaveBeenCalledTimes(2));
    await confirmWave(user, 2);

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(2);
    const [, first] = service.runCohortFlatten.mock.calls[0];
    const [, second] = service.runCohortFlatten.mock.calls[1];
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
  });

  it('dispatches nothing when the drawer opened against a cold directory', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const service = fakeService([QQQ_COHORT]);
    await open(service, { directory });
    await screen.findAllByRole('checkbox');

    expect(screen.getByText(/no known binding when the action was opened/i)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Review flatten of 2' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  it('has no detectable accessibility violations with cohorts and an outcome on screen', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), refused('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');
    await confirmWave(user, 2);
    await screen.findByRole('region', { name: 'Flatten outcome' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
