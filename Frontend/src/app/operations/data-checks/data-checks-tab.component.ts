import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { CurrentUrlService } from '../../shell/current-url.service';
import { WorkspaceTabListComponent } from '../../shared/workspace/workspace-tab-list.component';
import { DATA_CHECKS, dataCheckOf, dataCheckRoute } from './data-checks';

/**
 * Operations → Data checks: whether the numbers we hold agree with the numbers
 * TradingView shows. A strip over the three checks, each a child route, so
 * moving between them never re-creates this shell.
 *
 * Presentation only. Opening a check starts nothing: each check runs, uploads
 * and downloads from its own explicit controls.
 */
@Component({
  selector: 'app-operations-data-checks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, WorkspaceTabListComponent],
  templateUrl: './data-checks-tab.component.html',
  styleUrl: './data-checks-tab.component.scss',
})
export class DataChecksTabComponent {
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly checks = DATA_CHECKS.map((check) => ({ ...check, route: dataCheckRoute(check.id) }));
  protected readonly activeCheck = computed(() => dataCheckOf(this.currentUrl()));
}
