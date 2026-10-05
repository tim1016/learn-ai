import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import type { FillRow, RebuildRequest } from '../lib/lake-readiness';
import type { RebuildProgress } from '../readiness-banner/readiness-banner.component';

/** How many symbols the banner names before it counts the rest. */
const NAMED = 5;

/**
 * Says that symbols hold a first and a last session with sessions missing
 * between them, which a held span alone does not show, and that a backfill
 * fetches what is missing.
 *
 * Shown only while a backfill can fill something; a gap too old to fetch is
 * named in the symbols table, where no button promises a fix.
 */
@Component({
  selector: 'app-gap-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './gap-banner.component.html',
  styleUrls: ['../readiness-banner/readiness-banner.component.scss', './gap-banner.component.scss'],
  imports: [AssetIdentityComponent, DecimalPipe],
})
export class GapBannerComponent {
  /** The symbols with a gap a backfill can fill. */
  readonly rows = input.required<readonly FillRow[]>();
  /** True while any backfill runs, so a second one cannot be started over it. */
  readonly busy = input(false);
  readonly progress = input<RebuildProgress | null>(null);

  /** One backfill per symbol, each over that symbol's own gaps. */
  readonly fill = output<readonly RebuildRequest[]>();

  protected readonly named = computed(() => this.rows().slice(0, NAMED));
  protected readonly unnamed = computed(() => Math.max(0, this.rows().length - NAMED));

  protected readonly title = computed(() => {
    const count = this.rows().length;
    return count === 1
      ? '1 symbol has sessions missing between its first and last'
      : `${count} symbols have sessions missing between their first and last`;
  });

  protected fillAll(): void {
    this.fill.emit(this.rows().map((row) => row.fill));
  }
}
