import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { InstrumentContextService } from '../../instrument-context.service';
import { ArchiveChainComponent } from './archive/archive-chain.component';
import { ChainModeSwitchComponent } from './chain-mode-switch.component';
import { chainModeOf } from './chain-request';
import { ChainRequestService } from './chain-request.service';
import { SnapshotChainComponent } from './snapshot/snapshot-chain.component';

/** Live IBKR is not on this chain yet; the strip above says why, and nothing else is shown in its place. */
const LIVE_STATE: WorkspaceStateContent = {
  kind: 'unavailable',
  happened: 'Now · Live IBKR is not available on this chain yet.',
  next: 'Choose Now · Snapshot or Past · Archive above. Nothing has been switched for you.',
};

/** A past instant is never read from the live feed, and the present's chain is never shown under a past date. */
const PAST_LIVE_STATE: WorkspaceStateContent = {
  kind: 'unavailable',
  happened: 'Past · Live IBKR is not available.',
  next: 'Choose Past · Archive to read a past date, or Now · Snapshot for the current chain. Nothing has been switched for you.',
};

/**
 * The Options tab's chain (`/markets/:symbol/options/chain`, PRD #3490 R11): one chain
 * presentation over a source the address names.
 *
 * The address is `?source=(snapshot|live|archive)` with the `expiry`, and for the past a
 * session `date` and an instant `at`. That is the tab's own context: it shares no key with the
 * instrument's window, so a change here never moves the chart, and the chart tab still has its
 * window when the operator returns to it. A source the address does not name usably is said in
 * place and loads nothing. Choosing a mode keeps the expiry, so the contract is the same one
 * under another source; only the source's own adapter is mounted, so leaving one tears down its
 * requests and nothing it still has in flight can answer into the next chain.
 */
@Component({
  selector: 'app-options-chain',
  imports: [ChainModeSwitchComponent, SnapshotChainComponent, ArchiveChainComponent, WorkspaceStateBlockComponent],
  providers: [ChainRequestService],
  templateUrl: './options-chain.component.html',
  styleUrl: './options-chain.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class OptionsChainComponent {
  protected readonly requests = inject(ChainRequestService);
  protected readonly symbol = inject(InstrumentContextService).symbol;

  protected readonly request = this.requests.request;
  protected readonly active = computed(() => chainModeOf(this.request()));

  protected readonly liveState = LIVE_STATE;
  protected readonly pastLiveState = PAST_LIVE_STATE;
  protected readonly invalidState = computed<WorkspaceStateContent | null>(() => {
    const request = this.request();
    return request.kind === 'invalid'
      ? {
          kind: 'invalid',
          happened: 'The chain asked for in this address cannot be read, so nothing is loaded.',
          why: request.problems.join(' '),
          next: 'Choose a source above. Nothing is substituted for what the address asked for.',
        }
      : null;
  });
}
