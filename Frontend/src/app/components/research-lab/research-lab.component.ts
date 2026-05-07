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
import { filter, map } from 'rxjs/operators';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { NavGroup, RESEARCH_LAB_NAV } from './research-lab-nav.config';

interface NavState {
  data: Record<string, unknown>;
  /** Matched route config path of the deepest active route, e.g.
   *  `'features/validate'` or `'signals/engine'`. */
  path: string | null;
}

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

  /** Refreshes on every successful navigation; powers title, subtitle,
   *  and which parent group's children should be shown in row 2. */
  private readonly navState = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map(() => this.snapshotNavState()),
    ),
    { initialValue: this.snapshotNavState() },
  );

  readonly title = computed<string>(
    () => (this.navState().data['title'] as string | undefined) ?? 'Research Lab',
  );
  readonly subtitle = computed<string>(
    () => (this.navState().data['subtitle'] as string | undefined) ?? '',
  );

  /** Group whose child is currently active. Falls back to the first group
   *  for the brief moment between landing on `/research-lab` and the
   *  redirect to `features/validate` resolving. */
  readonly activeGroup = computed<NavGroup>(() => {
    const path = this.navState().path;
    const match = this.groups.find((g) =>
      g.items.some((item) => item.path === path),
    );
    return match ?? this.groups[0];
  });

  /** Click handler for a parent pill — navigates to its first child.
   *  No-op when the clicked parent is already the active one, so
   *  re-clicking doesn't bounce the user out of a sibling page. */
  selectGroup(group: NavGroup): void {
    if (group === this.activeGroup()) return;
    const first = group.items[0];
    if (!first) return;
    void this.router.navigate([first.path], { relativeTo: this.route });
  }

  private snapshotNavState(): NavState {
    let r: ActivatedRoute | null = this.route;
    while (r?.firstChild) r = r.firstChild;
    return {
      data: r?.snapshot?.data ?? {},
      path: r?.snapshot?.routeConfig?.path ?? null,
    };
  }
}
