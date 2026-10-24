import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../../../components/broker/v2-panel/lib/broker-v2-panel.service';
import type { BotCatalogView } from '../../../components/broker/v2-panel/lib/broker-v2-panel.types';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { provideFleetDirectory, TEST_ACCOUNT_ID, TEST_CLERK_ID, testLane } from '../../../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../../../fleet/fleet-directory.types';
import { GOLDEN_ID, sha } from '../../testing/fixtures';
import { OptionsSetupBotsService } from './options-setup-bots.service';

function bot(sid: string, changes: Partial<BotCatalogView> = {}): BotCatalogView {
  return {
    account_id: TEST_ACCOUNT_ID,
    broker: 'alpaca',
    desired_state: 'RUNNING',
    exposure: null,
    fills_today: null,
    day_pnl: null,
    group: 'running',
    last_activity_at_ms: null,
    mode: 'trade',
    needs_attention: false,
    open_pnl: null,
    phase: 'ON_DUTY',
    realized_pnl_today: null,
    running: true,
    status_explanation: '',
    status_label: 'Running',
    strategy_instance_id: sid,
    strategy_key: 'options',
    strategy_label: `Bot ${sid}`,
    symbol: 'SPY',
    world_label: 'Paper',
    bot_kind: 'options',
    options_bot: { golden_setup_id: GOLDEN_ID, rules_digest: 'r', setup_sha256: sha('1'), setup: {} as never },
    ...changes,
  } as BotCatalogView;
}

function read(lanes: LaneDescriptor[], catalogs: Record<string, () => Promise<BotCatalogView[]>>) {
  const fleet = provideFleetDirectory({ observed_at_ms: 1, clerks: lanes });
  const getCatalog = vi.fn(async (target: { clerkId: string }) => (catalogs[target.clerkId] ?? (async () => []))());
  TestBed.configureTestingModule({
    providers: [
      { provide: fleet.provide, useValue: fleet.useValue },
      { provide: BrokerV2PanelService, useValue: { getCatalog } },
    ],
  });
  return { getCatalog, service: TestBed.inject(OptionsSetupBotsService) };
}

describe('the options bots of the fleet', () => {
  it('lists every options bot of every ready account, with the exact content and version each was started from', async () => {
    const { service, getCatalog } = read([testLane()], { [TEST_CLERK_ID]: async () => [bot('sid-1'), bot('sid-2', { options_bot: null, bot_kind: 'signal' })] });

    const bots = await service.read();

    expect(bots).toMatchObject({ kind: 'known', bots: [{ sid: 'sid-1', label: 'Bot sid-1', world: 'Paper', setupSha256: sha('1'), goldenSetupId: GOLDEN_ID }] });
    expect(bots.kind === 'known' && bots.bots[0]?.route).toEqual(['/brokers', 'alpaca', 'clerks', TEST_CLERK_ID, 'accounts', TEST_ACCOUNT_ID, 'bots', 'sid-1']);
    expect(getCatalog).toHaveBeenCalledTimes(1);
  });

  it('knows there are none when every account was read and none runs an options bot', async () => {
    const { service } = read([testLane()], { [TEST_CLERK_ID]: async () => [bot('sid-1', { options_bot: null, bot_kind: 'signal' })] });

    expect(await service.read()).toEqual({ kind: 'known', bots: [] });
  });

  it('is unknown, not none, when one account cannot be read, though another answered', async () => {
    const live = testLane({ clerk_id: 'clrk_live', display_label: 'Live', provider_summary: { confirmed_account_id: 'LIVE1' } });
    const { service } = read([testLane(), live], {
      [TEST_CLERK_ID]: async () => [bot('sid-1')],
      clrk_live: async () => {
        throw new Error('lane down');
      },
    });

    expect(await service.read()).toEqual({ kind: 'unknown', reason: 'The bots of the account “Live” could not be read.' });
  });

  it('is unknown when an account is not ready, without asking its lane', async () => {
    const draining = testLane({ clerk_id: 'clrk_old', display_label: 'Old', lifecycle_state: 'draining' });
    const { service, getCatalog } = read([testLane(), draining], {});

    expect(await service.read()).toEqual({ kind: 'unknown', reason: 'The account “Old” is not ready, so its bots cannot be listed.' });
    expect(getCatalog).not.toHaveBeenCalled();
  });

  it('is unknown when the list of accounts cannot be read', async () => {
    TestBed.configureTestingModule({
      providers: [
        { provide: FleetDirectoryService, useValue: { ensureLoaded: () => Promise.reject(new Error('down')) } },
        { provide: BrokerV2PanelService, useValue: { getCatalog: vi.fn() } },
      ],
    });

    expect(await TestBed.inject(OptionsSetupBotsService).read()).toEqual({ kind: 'unknown', reason: 'The list of broker accounts could not be read.' });
  });
});
