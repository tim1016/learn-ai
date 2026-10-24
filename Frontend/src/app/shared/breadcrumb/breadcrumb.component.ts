import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { menuTrailFor } from '../../shell/app-menu';
import { CurrentUrlService } from '../../shell/current-url.service';
import type { BreadcrumbItem } from './breadcrumb-item';

/**
 * The trail back from a page, drawn by both page headers (`app-page-header`
 * and `app-workspace-header`) so there is one rule for it (PRD #3490 R01).
 *
 * With no `items`, the trail is the page's own place in the menu: its group,
 * then the listed page it sits inside when `heading` names something deeper.
 * A host with a path the menu cannot know (a list, then one record of it)
 * passes `items`, and those replace the menu's.
 */
@Component({
  selector: 'app-breadcrumb',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './breadcrumb.component.html',
  styleUrl: './breadcrumb.component.scss',
})
export class BreadcrumbComponent {
  /** The page's own heading: a menu page the heading already names adds no crumb. */
  readonly heading = input.required<string>();
  /** A hand-built trail. `null` leaves the trail to the menu; `[]` draws none. */
  readonly items = input<readonly BreadcrumbItem[] | null>(null);

  private readonly pageUrl = inject(CurrentUrlService).pageUrl;

  protected readonly trail = computed<readonly BreadcrumbItem[]>(() => {
    const own = this.items();
    if (own !== null) return own;
    const url = this.pageUrl();
    return url === null ? [] : menuTrailFor(url, this.heading());
  });
}
