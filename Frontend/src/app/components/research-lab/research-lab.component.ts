import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
} from '@angular/core';
import {
  ActivatedRoute,
  NavigationEnd,
  Router,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { filter, map, startWith } from 'rxjs/operators';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { RESEARCH_LAB_NAV } from './research-lab-nav.config';

@Component({
  selector: 'app-research-lab',
  imports: [
    RouterLink,
    RouterLinkActive,
    RouterOutlet,
    PageHeaderComponent,
  ],
  templateUrl: './research-lab.component.html',
  styleUrls: ['./research-lab.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResearchLabComponent {
  readonly groups = RESEARCH_LAB_NAV;

  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  /** Snapshot of the deepest active route's `data`, refreshed on every
   *  successful navigation. Page header pulls title + subtitle from here. */
  private readonly activeData = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      startWith(null),
      map(() => this.deepestData()),
    ),
    { initialValue: this.deepestData() },
  );

  readonly title = computed<string>(
    () => (this.activeData()?.['title'] as string | undefined) ?? 'Research Lab',
  );
  readonly subtitle = computed<string>(
    () => (this.activeData()?.['subtitle'] as string | undefined) ?? '',
  );

  private deepestData(): Record<string, unknown> {
    let r: ActivatedRoute | null = this.route;
    while (r?.firstChild) r = r.firstChild;
    return r?.snapshot?.data ?? {};
  }
}
