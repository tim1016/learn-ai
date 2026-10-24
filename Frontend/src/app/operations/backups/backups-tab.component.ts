import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { OptionArchiveService } from '../../shared/option-archive/option-archive.service';
import { TimestampDisplayComponent } from '../../shared/timestamp/timestamp-display.component';

/**
 * Operations → Backups: whether the lake can be recovered. The latest backup
 * with its generation and destination space, and the weekly restore check with
 * the last time it passed.
 *
 * Read-only, from the same status the Option archive tab reads, read again every
 * time the tab opens and dated. A backup that is behind concerns recoverability;
 * it does not by itself make a stored price wrong, so this tab never speaks for
 * current data.
 */
@Component({
  selector: 'app-operations-backups',
  imports: [TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './backups-tab.component.html',
  styleUrl: './backups-tab.component.scss',
})
export class BackupsTabComponent {
  readonly archive = inject(OptionArchiveService);

  constructor() {
    this.archive.refresh();
  }
}
