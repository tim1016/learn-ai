import { inject, Injectable } from '@angular/core';

import { BrokerV2PanelService } from '../../../components/broker/v2-panel/lib/broker-v2-panel.service';
import type { BotCatalogView } from '../../../components/broker/v2-panel/lib/broker-v2-panel.types';
import { accountWorkspaceBotRoute } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount, laneIsReady } from '../../../fleet/fleet-directory.types';
import { resourceTarget } from '../../../fleet/resource-target';

/** One options bot, and the exact setup content and approved version it was started from. */
export interface OptionsBotOfFleet {
  readonly sid: string;
  readonly label: string;
  readonly world: string;
  readonly status: string;
  readonly group: BotCatalogView['group'];
  readonly setupSha256: string;
  readonly goldenSetupId: string;
  readonly route: readonly string[];
}

/**
 * Every options bot on the fleet, or that this cannot be said. A count made
 * from the lanes that happened to answer would read as "none" for the lanes
 * that did not, so one lane that cannot be read makes the whole answer
 * unknown, and the reason says which.
 */
export type OptionsBotsRead =
  | { readonly kind: 'known'; readonly bots: readonly OptionsBotOfFleet[] }
  | { readonly kind: 'unknown'; readonly reason: string };

/**
 * The bots behind an options setup's Overview, from the reads the account
 * workspace already makes (the fleet directory, then each ready lane's bot
 * catalog). It starts nothing and reads each lane once.
 */
@Injectable({ providedIn: 'root' })
export class OptionsSetupBotsService {
  private readonly fleet = inject(FleetDirectoryService);
  private readonly panel = inject(BrokerV2PanelService);

  async read(): Promise<OptionsBotsRead> {
    let lanes;
    try {
      lanes = (await this.fleet.ensureLoaded()).clerks.filter((lane) => lane.broker === 'alpaca');
    } catch {
      return { kind: 'unknown', reason: 'The list of broker accounts could not be read.' };
    }
    const unreadable = lanes.find((lane) => !laneIsReady(lane) || laneConfirmedAccount(lane) === null);
    if (unreadable !== undefined) {
      return { kind: 'unknown', reason: `The account “${unreadable.display_label}” is not ready, so its bots cannot be listed.` };
    }
    const bots: OptionsBotOfFleet[] = [];
    for (const lane of lanes) {
      const accountId = laneConfirmedAccount(lane);
      if (accountId === null) continue;
      const target = resourceTarget('alpaca', lane.clerk_id, {
        accountId,
        bindingGeneration: lane.effective_binding_generation,
        routingEpoch: lane.routing_epoch,
      });
      let catalog: BotCatalogView[];
      try {
        catalog = await this.panel.getCatalog(target);
      } catch {
        return { kind: 'unknown', reason: `The bots of the account “${lane.display_label}” could not be read.` };
      }
      for (const bot of catalog) {
        if (bot.options_bot == null) continue;
        bots.push({
          sid: bot.strategy_instance_id,
          label: bot.strategy_label,
          world: bot.world_label,
          status: bot.status_label,
          group: bot.group,
          setupSha256: bot.options_bot.setup_sha256,
          goldenSetupId: bot.options_bot.golden_setup_id,
          route: accountWorkspaceBotRoute({ broker: 'alpaca', clerkId: lane.clerk_id, accountId }, bot.strategy_instance_id).commands,
        });
      }
    }
    return { kind: 'known', bots };
  }
}
