import {
  ChangeDetectionStrategy,
  Component,
  input,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { RouterModule } from "@angular/router";

export interface SubNavItem {
  label: string;
  /** RouterLink path — absolute works well for section-level nav. */
  route: string;
  /** PrimeIcons class, e.g. "pi pi-chart-line". */
  icon?: string;
  /** Shown as a pill to the right of the label (e.g. "beta"). */
  badge?: string;
}

/**
 * Section-level sub-nav.
 *
 * Renders a horizontal tab bar that sits between the top app shell and
 * the page content. Use it to promote heavy in-page tabs to route-level
 * navigation — e.g. Research Lab's Validate / Inspect / Reference split.
 *
 * Routing uses Angular's ``routerLinkActive`` so the "current" tab is
 * inferred from the URL without the host tracking active state.
 */
@Component({
  selector: "app-section-sub-nav",
  imports: [CommonModule, RouterModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="sub-nav" role="navigation" [attr.aria-label]="ariaLabel()">
      @if (section(); as s) {
        <span class="sub-nav__section">{{ s }}</span>
      }
      <div class="sub-nav__tabs">
        @for (item of items(); track item.route) {
          <a
            class="sub-nav__tab"
            [routerLink]="item.route"
            routerLinkActive="sub-nav__tab--active"
            [routerLinkActiveOptions]="{ exact: exactMatch() }"
          >
            @if (item.icon) {
              <i [class]="item.icon" class="sub-nav__icon" aria-hidden="true"></i>
            }
            <span>{{ item.label }}</span>
            @if (item.badge) {
              <span class="sub-nav__badge">{{ item.badge }}</span>
            }
          </a>
        }
      </div>
    </nav>
  `,
  styleUrls: ["./section-sub-nav.component.scss"],
})
export class SectionSubNavComponent {
  readonly items = input.required<readonly SubNavItem[]>();
  readonly section = input<string | null>(null);
  readonly ariaLabel = input<string>("Section navigation");
  readonly exactMatch = input<boolean>(false);
}
