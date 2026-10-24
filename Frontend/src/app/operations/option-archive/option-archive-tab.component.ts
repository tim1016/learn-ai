import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { needsAttention, OptionArchiveService } from '../../shared/option-archive/option-archive.service';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';

/**
 * Operations → Option archive: whether the sessions an options run needs are
 * preserved, per underlying, with capture and evening-observation health.
 *
 * Read-only: the status is read when the tab opens and again on Refresh. Backup
 * and restore-check evidence is the Backups tab's, from the same status.
 */
@Component({
  selector: 'app-operations-option-archive',
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-archive-tab.component.html',
  styleUrl: './option-archive-tab.component.scss',
})
export class OptionArchiveTabComponent {
  readonly archive = inject(OptionArchiveService);
  readonly needsAttention = needsAttention;
}
