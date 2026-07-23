import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink, NavigationEnd } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { filter } from 'rxjs/operators';
import { BrokerBannerComponent } from './broker-banner.component';

interface NavItem {
  label: string;
  route: string;
  deprecated?: boolean;
}

interface NavGroup {
  id: string;
  title: string;
  /** PrimeIcons class, e.g. 'pi pi-chart-line' */
  icon: string;
  items: NavItem[];
}

/**
 * Static information architecture.
 *
 * Reconciles the previous 7-group menubar into 5 sidebar groups per the
 * design bundle. All existing routes are preserved.
 */
const NAV: NavGroup[] = [
  {
    id: 'data-lab',
    title: 'Data Lab',
    icon: 'pi pi-database',
    items: [
      { label: 'Data Lab', route: '/data-lab' },
      { label: 'Indicator Report', route: '/indicator-report' },
    ],
  },
  {
    id: 'options',
    title: 'Options',
    icon: 'pi pi-sliders-h',
    items: [
      { label: 'Options Lab', route: '/options-lab' },
      { label: 'Options Chain (Live)', route: '/broker/options-chain' },
      { label: 'Options Surface (3D)', route: '/broker/options-surface' },
      { label: 'Pricing Lab', route: '/pricing-lab' },
    ],
  },
  {
    id: 'research',
    title: 'Research',
    icon: 'pi pi-compass',
    items: [
      { label: 'Research Lab', route: '/research-lab' },
      { label: 'Golden Fixtures', route: '/golden-fixtures' },
    ],
  },
  {
    id: 'edge',
    title: 'Edge Analysis',
    icon: 'pi pi-bolt',
    items: [
      { label: 'Overview', route: '/edge' },
      { label: 'Realized vs IV', route: '/edge/realized-vs-iv' },
      { label: 'Cross-Asset', route: '/edge/cross-asset' },
      { label: 'Regimes', route: '/edge/regimes' },
    ],
  },
  {
    id: 'interactive-broker',
    title: 'Interactive Broker',
    icon: 'pi pi-link',
    items: [
      { label: 'Accounts', route: '/broker/accounts' },
      { label: 'Orders', route: '/broker/orders' },
      { label: 'Session Mirror', route: '/broker/session-mirror' },
      { label: 'Deploy', route: '/broker/deploy' },
      { label: 'Bots', route: '/broker/bots' },
      { label: 'Bot Manual', route: '/broker/bot-manual' },
    ],
  },
  {
    id: 'alpaca',
    title: 'Alpaca',
    icon: 'pi pi-link',
    items: [
      { label: 'Accounts', route: '/brokers/alpaca' },
    ],
  },
  {
    id: 'design-lab',
    title: 'Design Lab',
    icon: 'pi pi-palette',
    items: [
      { label: 'Desert Oasis', route: '/broker/desert-oasis' },
      { label: 'Bot Sprites', route: '/broker/bot-sprites' },
    ],
  },
  {
    id: 'strategy-lab',
    title: 'Strategy Lab',
    icon: 'pi pi-briefcase',
    items: [
      { label: 'Strategy Validation', route: '/strategy-validation' },
      // PR B.5 (2026-05-19) — Engine Lab subsumes the standalone
      // LEAN Lab via the Engine dropdown; /lean-lab redirects here.
      { label: 'Strategy Spec', route: '/spec-strategy' },
      { label: 'Engine Lab', route: '/engine' },
    ],
  },
  {
    id: 'documentation',
    title: 'Documentation',
    icon: 'pi pi-book',
    items: [
      { label: 'Strategy Docs', route: '/strategy-docs' },
      { label: 'Indicator Reference', route: '/data-lab-docs' },
      { label: 'Pipeline Docs', route: '/data-quality-docs' },
      { label: 'Indicator Reliability', route: '/docs/indicator-reliability-methodology' },
      { label: 'Signal Engine', route: '/docs/signal-engine-methodology' },
    ],
  },
];

const NAV_ROUTES = NAV.flatMap((group) => group.items.map((item) => item.route)).sort(
  (left, right) => right.length - left.length,
);

@Component({
  selector: 'app-sidebar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, RouterLink, BrokerBannerComponent],
  styleUrl: './app-sidebar.component.scss',
  template: `
    <aside class="sidebar">
      <div class="brand">
        <svg width="18" height="22" viewBox="0 0 22 26" aria-hidden="true">
          <rect x="9" y="0" width="4" height="26" fill="#5a6178" />
          <rect x="4" y="5" width="14" height="14" fill="#00c896" rx="1" />
        </svg>
        <span class="wordmark">quant<span class="slash">/</span>lab</span>
      </div>

      <div class="search-wrap">
        <i class="pi pi-search search-icon" aria-hidden="true"></i>
        <input
          #searchInput
          type="text"
          class="search-input"
          placeholder="Jump to…"
          aria-label="Search navigation"
          [ngModel]="query()"
          (ngModelChange)="query.set($event)"
        />
        <span class="search-kbd mono">⌘K</span>
      </div>

      <nav class="nav-scroll" role="navigation">
        @if (filtered(); as matches) {
          <div class="flat-matches">
            @for (m of matches; track m.route) {
              <a
                class="nav-link"
                [class.active]="isActive(m.route)"
                [routerLink]="m.route"
                (click)="query.set('')"
              >
                <span class="nav-link-label">{{ m.label }}</span>
                <span class="nav-link-group mono">{{ m.groupTitle }}</span>
              </a>
            }
            @if (matches.length === 0) {
              <div class="empty">No matches</div>
            }
          </div>
        } @else {
          @for (g of groups; track g.id) {
            <div class="nav-group">
              <button
                type="button"
                class="nav-group-header"
                [class.has-active]="groupHasActive(g)"
                (click)="toggleGroup(g.id)"
                [attr.aria-expanded]="openGroups()[g.id] === true"
              >
                <i [class]="g.icon + ' group-icon'" aria-hidden="true"></i>
                <span class="group-title">{{ g.title }}</span>
                <i
                  class="pi pi-chevron-right chevron"
                  [class.open]="openGroups()[g.id] === true"
                  aria-hidden="true"
                ></i>
              </button>

              @if (openGroups()[g.id]) {
                <div class="nav-group-items">
                  @for (item of g.items; track item.route) {
                    <a
                      class="nav-link"
                      [class.active]="isActive(item.route)"
                      [routerLink]="item.route"
                    >
                      <span class="nav-link-label">{{ item.label }}</span>
                      @if (item.deprecated) {
                        <span class="nav-link-badge mono">deprecated</span>
                      }
                    </a>
                  }
                </div>
              }
            </div>
          }
        }
      </nav>

      <div class="status-footer">
        <app-broker-banner />
      </div>
    </aside>
  `,
})
export class AppSidebarComponent {
  private router = inject(Router);
  private destroyRef = inject(DestroyRef);

  readonly groups = NAV;

  /** Current route URL — updated on NavigationEnd. Signal so child bindings refresh. */
  private currentUrl = signal<string>(this.router.url);

  /** Only the longest route match is active, so `/broker` does not shadow its children. */
  private activeRoute = computed<string | null>(() => {
    const url = this.navigationPath(this.currentUrl());
    return NAV_ROUTES.find((route) => url === route || url.startsWith(route + '/')) ?? null;
  });

  /** Open/closed state per group. Groups containing the active route auto-open. */
  openGroups = signal<Record<string, boolean>>(this.computeInitialOpenState());

  /** Search query string. Non-empty switches nav into flat-match mode. */
  query = signal<string>('');

  /**
   * When query is non-empty, return the matching items flattened across groups.
   * Null means "show the normal grouped tree."
   */
  filtered = computed<{ label: string; route: string; groupTitle: string }[] | null>(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return null;
    const matches: { label: string; route: string; groupTitle: string }[] = [];
    for (const g of NAV) {
      for (const it of g.items) {
        if ((it.label + ' ' + g.title).toLowerCase().includes(q)) {
          matches.push({ label: it.label, route: it.route, groupTitle: g.title });
        }
      }
    }
    return matches;
  });

  searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');

  constructor() {
    this.router.events
      .pipe(
        filter((e): e is NavigationEnd => e instanceof NavigationEnd),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(e => {
        this.currentUrl.set(e.urlAfterRedirects);
        // Auto-open the group containing the newly active route.
        this.openGroups.update(state => {
          const next = { ...state };
          for (const g of NAV) {
            if (this.groupContainsUrl(g, e.urlAfterRedirects)) {
              next[g.id] = true;
            }
          }
          return next;
        });
      });
  }

  /** ⌘K / Ctrl+K focuses the search input globally. */
  @HostListener('window:keydown', ['$event'])
  handleKeydown(event: KeyboardEvent): void {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      const el = this.searchInput()?.nativeElement;
      if (el) {
        el.focus();
        el.select();
      }
    }
  }

  toggleGroup(id: string): void {
    this.openGroups.update(state => ({ ...state, [id]: !state[id] }));
  }

  isActive(route: string): boolean {
    return this.activeRoute() === route;
  }

  groupHasActive(g: NavGroup): boolean {
    return g.items.some(it => this.isActive(it.route));
  }

  private groupContainsUrl(g: NavGroup, url: string): boolean {
    const path = this.navigationPath(url);
    return g.items.some(it => path === it.route || path.startsWith(it.route + '/'));
  }

  private navigationPath(url: string): string {
    return url.split(/[?#]/, 1)[0] ?? '';
  }

  private computeInitialOpenState(): Record<string, boolean> {
    const url = this.router.url;
    const state: Record<string, boolean> = {};
    for (const g of NAV) {
      state[g.id] = this.groupContainsUrl(g, url);
    }
    return state;
  }
}
