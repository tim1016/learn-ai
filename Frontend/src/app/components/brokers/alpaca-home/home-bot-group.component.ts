import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { BoundAccountWorkspaceAddress } from '../../../fleet/account-workspace';
import type { ChartBar, ChartFillMarker } from '../../broker/v2-panel/gallery/lib/gallery.types';
import type { StopPhase } from './home-bot-action.component';
import { HomeBotRowComponent } from './home-bot-row.component';
import { HomeBotTileComponent } from './home-bot-tile.component';
import type { HomeBot } from './home-bots';

/**
 * One group of Home's bots as the List or the Wall (PRD #2560 D11): the same
 * entries in the same order either way — the Wall only draws each as a chart
 * tile, with its candles and fills from the gallery live feed.
 */
@Component({
  selector: 'app-home-bot-group',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HomeBotRowComponent, HomeBotTileComponent],
  template: `
    <ul [class]="wall() ? 'home-group home-group--wall' : 'home-group'" [attr.aria-label]="label()">
      @for (entry of entries(); track entry.bot.strategy_instance_id) {
        <li>
          @if (wall()) {
            <app-home-bot-tile
              [entry]="entry"
              [account]="account()"
              [stopPhase]="stopPhases().get(entry.bot.strategy_instance_id) ?? null"
              [bars]="barsBySymbol().get(entry.bot.symbol) ?? []"
              [markers]="markersBySid().get(entry.bot.strategy_instance_id) ?? []"
              (stopRequested)="stopRequested.emit($event)"
            />
          } @else {
            <app-home-bot-row
              [entry]="entry"
              [account]="account()"
              [stopPhase]="stopPhases().get(entry.bot.strategy_instance_id) ?? null"
              (stopRequested)="stopRequested.emit($event)"
            />
          }
        </li>
      }
    </ul>
  `,
  styles: [`
    :host { display: block; }
    .home-group { display: grid; margin: 0; padding: 0; list-style: none; }
    .home-group--wall {
      grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
      gap: var(--space-3);
      padding: var(--space-3);
    }
  `],
})
export class HomeBotGroupComponent {
  readonly entries = input.required<readonly HomeBot[]>();
  readonly label = input.required<string>();
  readonly wall = input(false);
  readonly account = input.required<BoundAccountWorkspaceAddress>();
  readonly stopPhases = input<ReadonlyMap<string, StopPhase>>(new Map());
  readonly barsBySymbol = input<ReadonlyMap<string, readonly ChartBar[]>>(new Map());
  readonly markersBySid = input<ReadonlyMap<string, readonly ChartFillMarker[]>>(new Map());
  readonly stopRequested = output<string>();
}
