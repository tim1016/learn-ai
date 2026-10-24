import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';
import type { ReadState } from '../read-state';
import { QUALIFICATION_LIST_LIMIT } from '../../components/golden-search/golden-search.service';
import { QUALIFICATION_TONES } from '../signal-strategy-evidence';
import { SignalStrategyChipComponent } from '../signal-strategy-chip.component';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import type { QualifiedVersions } from '../signal-strategy-workspace.store';

/**
 * Golden Search's qualified versions, one symbol at a time. A version
 * approves one symbol with one exact configuration, and it is current only
 * while its status says ready: a stale, revoked or unverifiable version is
 * shown as that, beside the symbol it belongs to. Nothing here sums a
 * strategy's symbols into one verdict.
 */
@Component({
  selector: 'app-signal-strategy-qualified-versions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    LowerCasePipe,
    ReceiptLabelPipe,
    SignalStrategyChipComponent,
    SignalStrategySectionComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './qualified-versions.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyQualifiedVersionsComponent {
  readonly versions = input.required<ReadState<QualifiedVersions>>();
  /** Why Golden Search is not offered for this strategy, or `null` when it is. */
  readonly goldenRefusal = input<string | null | undefined>(null);
  readonly retry = output();

  protected readonly tones = QUALIFICATION_TONES;
  protected readonly listLimit = QUALIFICATION_LIST_LIMIT;
}
