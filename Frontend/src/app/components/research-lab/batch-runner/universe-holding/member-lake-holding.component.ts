import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import type { InstrumentDataRequest } from '../../../../shared/data-lake';
import { createLakeHolding } from '../../../../shared/lake-study/create-lake-holding';
import { LakeHoldingNoticeComponent } from '../../../../shared/lake-study/lake-holding-notice.component';
import { describeLakeHolding } from '../../../../shared/lake-study/lake-holding';
import { LAKE_STUDY_TREE } from '../../../../shared/lake-study/lake-study';

/**
 * One member's lake holdings: the shared count and capture (`createLakeHolding`) for this symbol over the entered
 * window, under the member's name. It draws nothing while the lake holds every session the run needs, so a universe
 * lists only the members that lack some. The capture it offers is that member's, over the span the server named.
 */
@Component({
  selector: 'app-member-lake-holding',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, LakeHoldingNoticeComponent],
  template: `
    @if (shown()) {
      <div class="member-holding" [attr.data-testid]="'holding-' + symbol()">
        <app-asset-identity [symbol]="symbol()" size="xs" />
        <app-lake-holding-notice [holding]="holding" />
      </div>
    }
  `,
  styles: `
    .member-holding {
      display: grid;
      gap: 0.375rem;
    }
  `,
})
export class MemberLakeHoldingComponent {
  readonly symbol = input.required<string>();
  /** `YYYY-MM-DD`, both included: the window the run is given. Empty while the page's inputs are incomplete. */
  readonly from = input.required<string>();
  readonly to = input.required<string>();

  /** The count and the one capture that may be running, read once per distinct symbol and window. */
  readonly holding = createLakeHolding(
    computed<InstrumentDataRequest | null>(() => {
      const from = this.from();
      const to = this.to();
      return from === '' || to === '' ? null : { symbol: this.symbol(), from, to, tree: LAKE_STUDY_TREE };
    }),
  );

  protected readonly shown = computed(
    () => describeLakeHolding(this.holding.reading(), this.holding.capture(), this.holding.progress()) !== null,
  );
}
