import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  CohortActionResult,
  CohortArchiveLeg,
  CohortArchiveView,
} from '../lib/broker-v2-panel.types';
import { CohortArchiveDrawerComponent } from './cohort-archive-drawer.component';

function leg(overrides: Partial<CohortArchiveLeg> = {}): CohortArchiveLeg {
  return {
    strategy_instance_id: 'spy-done-1',
    enabled: true,
    revision: 1,
    concurrency_token: 'token-1',
    blocker_headline: null,
    ...overrides,
  };
}

function view(legs: CohortArchiveLeg[]): CohortArchiveView {
  return {
    account_id: 'PA1',
    observed_at_ms: 1_700_000_000_000,
    cohorts: [
      {
        strategy_key: 'deployment_validation',
        strategy_label: 'Deployment Validation',
        symbol: 'SPY',
        legs,
        enabled_count: legs.filter((item) => item.enabled).length,
      },
    ],
  };
}

function result(overrides: Partial<CohortActionResult> = {}): CohortActionResult {
  return {
    account_id: 'PA1',
    receipt_id: 'sweep-1',
    recorded_at_ms: 1_700_000_000_000,
    legs: [],
    applied_count: 1,
    replayed_count: 0,
    refused_count: 0,
    failed_count: 0,
    ...overrides,
  };
}

function fakeService(legs: CohortArchiveLeg[], batch = result()) {
  return {
    getCohortArchiveView: vi.fn().mockResolvedValue(view(legs)),
    runCohortArchive: vi.fn().mockResolvedValue(batch),
  };
}

async function open(service: ReturnType<typeof fakeService>) {
  await render(CohortArchiveDrawerComponent, {
    inputs: { visible: true, broker: 'alpaca', accountId: 'PA1' },
    providers: [{ provide: BrokerV2PanelService, useValue: service }],
  });
}

afterEach(() => vi.restoreAllMocks());

describe('CohortArchiveDrawerComponent', () => {
  it('shows a bot it cannot archive, with the reason, rather than hiding it', async () => {
    // A surface whose job is "show me what I can clear" must not quietly
    // under-report the roster: a hidden bot reads as an absent one.
    const service = fakeService([
      leg(),
      leg({
        strategy_instance_id: 'spy-held-1',
        enabled: false,
        revision: null,
        concurrency_token: null,
        blocker_headline: 'This bot still holds custody.',
      }),
    ]);

    await open(service);

    expect(await screen.findByText('spy-held-1')).toBeTruthy();
    expect(screen.getByText('This bot still holds custody.')).toBeTruthy();
    const blocked = screen.getAllByRole('checkbox')[1] as HTMLInputElement;
    expect(blocked.disabled).toBe(true);
  });

  it('refuses to submit until the operator types the confirmation token', async () => {
    const service = fakeService([leg()]);
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    const submit = screen.getByRole('button', { name: /Archive 1/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');

    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it('sends exactly the checked legs, each with the identity it was presented with', async () => {
    // ADR 0051 Decision 2: membership is explicit. The server must execute
    // what the operator actually saw, not a set it re-derived.
    const service = fakeService([
      leg(),
      leg({
        strategy_instance_id: 'spy-done-2',
        revision: 4,
        concurrency_token: 'token-2',
      }),
    ]);
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[1]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'archive');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect(service.runCohortArchive).toHaveBeenCalledTimes(1);
    const [, , request] = service.runCohortArchive.mock.calls[0];
    expect(request.legs).toEqual([
      { strategy_instance_id: 'spy-done-2', revision: 4, concurrency_token: 'token-2' },
    ]);
  });

  it('reports every leg outcome, naming the ones that did not apply', async () => {
    const service = fakeService(
      [leg()],
      result({
        applied_count: 1,
        refused_count: 1,
        legs: [
          {
            strategy_instance_id: 'spy-done-2',
            outcome: 'refused',
            result: null,
            error: {
              action_id: 'archive',
              outcome: 'conflict',
              receipt_id: null,
              recorded_at_ms: 1,
              message: 'This action changed since it was presented.',
              why: null,
              reason_code: null,
            },
          },
        ],
      }),
    );
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect((await screen.findByRole('alert')).textContent).toContain('refused 1');
    expect(screen.getByText(/This action changed since it was presented\./)).toBeTruthy();
  });

  it('says so plainly when nothing on the account can be archived', async () => {
    const service = fakeService([
      leg({ enabled: false, revision: null, concurrency_token: null, blocker_headline: null }),
    ]);

    await open(service);

    expect(await screen.findByText(/No bot on this account can be archived/)).toBeTruthy();
  });
});
