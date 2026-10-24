import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { ActivatedRoute, NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { filter, map } from 'rxjs/operators';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { RESEARCH_LAB_NAV } from './research-lab-nav.config';

/**
 * The sub-nav shell for the pages Research Lab still hosts (see
 * `RESEARCH_LAB_NAV`). The studies it used to open on live under `/research`.
 */
@Component({
  selector: 'app-research-lab',
  imports: [RouterLink, RouterLinkActive, RouterOutlet, PageHeaderComponent],
  templateUrl: './research-lab.component.html',
  styleUrls: ['./research-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResearchLabComponent {
  readonly items = RESEARCH_LAB_NAV;

  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** Refreshes on every successful navigation; powers the title. */
  private readonly routeData = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map(() => this.snapshotRouteData()),
    ),
    { initialValue: this.snapshotRouteData() },
  );

  readonly title = computed<string>(
    () => (this.routeData()['title'] as string | undefined) ?? 'Research Lab',
  );

  private snapshotRouteData(): Record<string, unknown> {
    let r: ActivatedRoute | null = this.route;
    while (r?.firstChild) r = r.firstChild;
    return r?.snapshot?.data ?? {};
  }
}
