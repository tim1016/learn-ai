import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AlpacaLaneModeChipComponent } from '../../components/brokers/alpaca-desk/alpaca-lane-mode-chip.component';
import { historyAccountName, historyWorldChip } from '../../components/brokers/alpaca-history/bot-history.service';
import { accountWorkspaceBotRoute } from '../../fleet/account-workspace';
import { FleetDirectoryService } from '../../fleet/fleet-directory.service';
import { laneConfirmedAccount } from '../../fleet/fleet-directory.types';
import { BotInstrumentComponent } from '../../shared/bot-instrument/bot-instrument.component';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';
import type { ReadState } from '../read-state';
import type { FleetRunningRead, RunningBotsAnswer } from '../signal-strategy-evidence';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';

/**
 * The strategy's bots that are running now, each with its account, its world
 * and when the fleet was read. The fleet read covers every account, and an
 * account it could not read is named: a read that missed one cannot call the
 * strategy idle, so it says unknown or "at least", never none.
 */
@Component({
  selector: 'app-signal-strategy-running-bots',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaLaneModeChipComponent,
    BotInstrumentComponent,
    RouterLink,
    SignalStrategySectionComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './running-bots.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyRunningBotsComponent {
  private readonly fleetDirectory = inject(FleetDirectoryService);

  readonly fleet = input.required<ReadState<FleetRunningRead>>();
  readonly answer = input.required<RunningBotsAnswer>();
  readonly retry = output();

  protected readonly unreadable = computed(() => {
    const state = this.fleet();
    if (state.kind !== 'ready') return [];
    return state.value.gaps.map((gap) => ({
      key: `${gap.clerk_id}/${gap.strategy_instance_id ?? ''}`,
      account: historyAccountName(this.fleetDirectory, gap.broker, gap.clerk_id, gap.account_id),
      reason: gap.reason,
    }));
  });

  protected readonly lines = computed(() =>
    this.answer().bots.map((bot) => {
      const lane = this.fleetDirectory.lane(bot.broker, bot.clerk_id);
      return {
        key: `${bot.clerk_id}/${bot.strategy_instance_id}`,
        bot,
        account: historyAccountName(this.fleetDirectory, bot.broker, bot.clerk_id, bot.account_id),
        world: historyWorldChip(bot),
        // A bot of the account's other world has no page to open; the backend says why.
        page:
          bot.page_unavailable_reason !== null
            ? null
            : accountWorkspaceBotRoute(
                {
                  broker: bot.broker,
                  clerkId: bot.clerk_id,
                  accountId: (lane === undefined ? null : laneConfirmedAccount(lane)) ?? bot.account_id,
                },
                bot.strategy_instance_id,
              ).commands,
      };
    }),
  );
}
