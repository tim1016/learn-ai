import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
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
  queryParams?: Record<string, string>;
  activePath?: string;
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
 * Contains current product surfaces only. Retired broker navigation remains
 * redirect-only in the route table and is intentionally absent from the UI.
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
    id: 'alpaca',
    title: 'Alpaca',
    icon: 'pi pi-link',
    items: [
      { label: 'Accounts', route: '/brokers/alpaca' },
      {
        label: 'Deploy',
        route: '/brokers/alpaca',
        queryParams: { deploy: '' },
        activePath: '/brokers/alpaca/deploy',
      },
      { label: 'Bots', route: '/brokers/alpaca/bots' },
      { label: 'Gallery', route: '/brokers/alpaca/gallery' },
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
    id: 'strategy-tools',
    title: 'Strategy Tools',
    icon: 'pi pi-briefcase',
    items: [
      { label: 'Strategy Validation', route: '/strategy-validation' },
      // Strategy Lab owns the one-strategy diagnostic and LEAN parity launch.
      { label: 'Strategy Spec', route: '/spec-strategy' },
      { label: 'Strategy Lab', route: '/strategy-lab' },
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
      { label: 'Legal Notices', route: '/legal/notices' },
    ],
  },
];

const NAV_ROUTES = NAV.flatMap((group) => group.items.map((item) => item.activePath ?? item.route)).sort(
  (left, right) => right.length - left.length,
);

const SIDEBAR_PINNED_STORAGE_KEY = 'quant-lab.sidebar.pinned';
const FLYOUT_CLOSE_DELAY_MS = 180;

@Component({
  selector: 'app-sidebar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, RouterLink, BrokerBannerComponent],
  styleUrl: './app-sidebar.component.scss',
  host: {
    '[class.sidebar--pinned]': 'pinned()',
    '[class.sidebar--search-open]': 'searchOpen()',
    '(window:keydown)': 'handleKeydown($event)',
  },
  template: `
    <aside class="sidebar" [class.sidebar--pinned]="pinned()">
      <div class="brand">
        <svg width="18" height="22" viewBox="0 0 22 26" aria-hidden="true">
          <rect x="9" y="0" width="4" height="26" fill="#5a6178" />
          <rect x="4" y="5" width="14" height="14" fill="#00c896" rx="1" />
        </svg>
        <span class="wordmark">quant<span class="slash">/</span>lab</span>
      </div>

      <div class="search-wrap">
        @if (pinned()) {
          <i class="pi pi-search search-icon" aria-hidden="true"></i>
        } @else {
          <button
            type="button"
            class="rail-search-trigger"
            aria-label="Search navigation"
            (click)="openSearch()"
          >
            <i class="pi pi-search" aria-hidden="true"></i>
          </button>
        }
        @if (pinned() || searchOpen()) {
          <input
            #searchInput
            type="text"
            class="search-input"
            placeholder="Jump to…"
            aria-label="Search navigation"
            [ngModel]="query()"
            (ngModelChange)="onQueryChanged($event)"
            (focus)="openSearch()"
          />
        }
        @if (pinned()) {
          <span class="search-kbd mono">⌘K</span>
        }
      </div>

      <nav class="nav-scroll" role="navigation">
        @if (filtered(); as matches) {
          <div class="flat-matches">
            @for (m of matches; track m.label) {
              <a
                class="nav-link"
                [class.active]="isActive(m)"
                [routerLink]="m.route"
                [queryParams]="m.queryParams"
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
            <div
              class="nav-group"
              [class.nav-group--flyout-open]="openFlyoutId() === g.id"
              (mouseleave)="scheduleFlyoutClose()"
              (focusin)="cancelFlyoutClose()"
              (focusout)="scheduleFlyoutClose()"
            >
              <button
                type="button"
                [id]="groupTriggerId(g.id)"
                class="nav-group-header"
                [class.has-active]="groupHasActive(g)"
                [class.rail-group-button]="!pinned()"
                (click)="onGroupActivated(g, $event)"
                (mouseenter)="openFlyout(g, $event)"
                (focus)="openFlyout(g, $event)"
                (keydown)="onGroupKeydown(g, $event)"
                [attr.aria-label]="pinned() ? null : g.title"
                [attr.aria-controls]="flyoutId(g.id)"
                [attr.aria-expanded]="pinned() ? openGroups()[g.id] === true : openFlyoutId() === g.id"
              >
                <i [class]="g.icon + ' group-icon'" aria-hidden="true"></i>
                <span class="group-title">{{ g.title }}</span>
                <i
                  class="pi pi-chevron-right chevron"
                  [class.open]="pinned() ? openGroups()[g.id] === true : openFlyoutId() === g.id"
                  aria-hidden="true"
                ></i>
              </button>

              @if (pinned() && openGroups()[g.id]) {
                <div class="nav-group-items">
                  @for (item of g.items; track item.label) {
                    <a
                      class="nav-link"
                      [class.active]="isActive(item)"
                      [routerLink]="item.route"
                      [queryParams]="item.queryParams"
                      (click)="onNavigationSelected()"
                    >
                      <span class="nav-link-label">{{ item.label }}</span>
                    </a>
                  }
                </div>
              }

              @if (!pinned() && openFlyoutId() === g.id) {
                <section
                  [id]="flyoutId(g.id)"
                  class="nav-flyout"
                  role="region"
                  [attr.aria-labelledby]="groupTriggerId(g.id)"
                  [style.top.px]="flyoutPosition().top"
                  [style.left.px]="flyoutPosition().left"
                  (mouseenter)="cancelFlyoutClose()"
                  (mouseleave)="scheduleFlyoutClose()"
                  (focusin)="cancelFlyoutClose()"
                  (focusout)="scheduleFlyoutClose()"
                  (keydown)="onFlyoutKeydown($event)"
                >
                  <h2 class="nav-flyout-title">{{ g.title }}</h2>
                  <div class="nav-flyout-items">
                    @for (item of g.items; track item.label) {
                      <a
                        class="nav-link"
                        [class.active]="isActive(item)"
                        [routerLink]="item.route"
                        [queryParams]="item.queryParams"
                        (click)="onNavigationSelected()"
                      >
                        <span class="nav-link-label">{{ item.label }}</span>
                      </a>
                    }
                  </div>
                </section>
              }
            </div>
          }
        }
      </nav>

      <div class="status-footer">
        <app-broker-banner [compact]="!pinned()" />
        <button
          type="button"
          class="sidebar-pin"
          [attr.aria-label]="pinned() ? 'Use compact navigation rail' : 'Pin expanded navigation sidebar'"
          [attr.aria-pressed]="pinned()"
          (click)="togglePinned()"
        >
          <i class="pi pi-thumbtack" aria-hidden="true"></i>
        </button>
      </div>
    </aside>
  `,
})
export class AppSidebarComponent {
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  readonly groups = NAV;

  /** Current route URL — updated on NavigationEnd. Signal so child bindings refresh. */
  private currentUrl = signal<string>(this.router.url);

  /** Only the longest route match is active, so `/broker` does not shadow its children. */
  private activeRoute = computed<string | null>(() => {
    const currentUrl = this.currentUrl();
    const url = this.navigationPath(currentUrl);
    if (url === '/brokers/alpaca' && this.router.parseUrl(currentUrl).queryParams['deploy'] !== undefined) {
      return '/brokers/alpaca/deploy';
    }
    const accountScopedBrokerSurface = url.match(
      /^\/brokers\/([^/]+)\/accounts\/[^/]+\/(deploy|bots|gallery)(?:\/|$)/,
    );
    if (accountScopedBrokerSurface) {
      const [, broker, surface] = accountScopedBrokerSurface;
      const navigationSlot = `/brokers/${broker}/${surface}`;
      if (NAV_ROUTES.includes(navigationSlot)) return navigationSlot;
    }
    return NAV_ROUTES.find((route) => url === route || url.startsWith(route + '/')) ?? null;
  });

  /** Open/closed state per group. Groups containing the active route auto-open. */
  readonly openGroups = signal<Record<string, boolean>>(this.computeInitialOpenState());

  /** Persisted presentation preference. Compact rail is the default. */
  readonly pinned = signal(this.readPinnedPreference());

  /** The only group currently expanded over the compact rail. */
  readonly openFlyoutId = signal<string | null>(null);

  /** Viewport coordinates keep the flyout outside the scroll container. */
  readonly flyoutPosition = signal({ top: 0, left: 0 });

  /** Search query string. Non-empty switches nav into flat-match mode. */
  readonly query = signal<string>('');

  /** Compact rail search is a temporary popover; pinned search stays inline. */
  readonly searchOpen = signal(false);

  /**
   * When query is non-empty, return the matching items flattened across groups.
   * Null means "show the normal grouped tree."
   */
  filtered = computed<(NavItem & { groupTitle: string })[] | null>(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return null;
    const matches: (NavItem & { groupTitle: string })[] = [];
    for (const g of NAV) {
      for (const it of g.items) {
        if ((it.label + ' ' + g.title).toLowerCase().includes(q)) {
          matches.push({ ...it, groupTitle: g.title });
        }
      }
    }
    return matches;
  });

  readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');

  private flyoutCloseTimer: ReturnType<typeof setTimeout> | null = null;
  private searchFocusTimer: ReturnType<typeof setTimeout> | null = null;
  private flyoutTrigger: HTMLElement | null = null;
  private suppressNextFlyoutOpen = false;

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
    this.destroyRef.onDestroy(() => {
      this.cancelFlyoutClose();
      if (this.searchFocusTimer !== null) clearTimeout(this.searchFocusTimer);
    });
  }

  /** ⌘K / Ctrl+K focuses the search input globally. */
  protected handleKeydown(event: KeyboardEvent): void {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.openSearch(true);
      return;
    }
    if (event.key === 'Escape' && this.openFlyoutId() !== null) {
      event.preventDefault();
      this.closeFlyout(true);
    }
  }

  protected togglePinned(): void {
    const next = !this.pinned();
    this.pinned.set(next);
    this.writePinnedPreference(next);
    this.closeFlyout();
    if (next) this.searchOpen.set(false);
  }

  protected onQueryChanged(query: string): void {
    this.query.set(query);
    if (!this.pinned()) this.searchOpen.set(true);
  }

  protected openSearch(selectContents = false): void {
    if (this.pinned()) {
      const input = this.searchInput()?.nativeElement;
      input?.focus();
      if (selectContents) input?.select();
      return;
    }
    this.searchOpen.set(true);
    if (this.searchFocusTimer !== null) clearTimeout(this.searchFocusTimer);
    this.searchFocusTimer = setTimeout(() => {
      const input = this.searchInput()?.nativeElement;
      input?.focus();
      if (selectContents) input?.select();
      this.searchFocusTimer = null;
    });
  }

  protected onGroupActivated(group: NavGroup, event: Event): void {
    if (this.pinned()) {
      this.toggleGroup(group.id);
      return;
    }
    this.openFlyout(group, event);
  }

  protected openFlyout(group: NavGroup, event: Event): void {
    if (this.pinned() || this.query().trim()) return;
    if (this.suppressNextFlyoutOpen && event.type === 'focus') {
      this.suppressNextFlyoutOpen = false;
      return;
    }
    const trigger = event.currentTarget;
    if (!(trigger instanceof HTMLElement)) return;
    this.cancelFlyoutClose();
    this.flyoutTrigger = trigger;
    const bounds = trigger.getBoundingClientRect();
    this.flyoutPosition.set({ top: Math.max(8, bounds.top), left: bounds.right + 8 });
    this.openFlyoutId.set(group.id);
  }

  protected scheduleFlyoutClose(): void {
    if (this.pinned() || this.openFlyoutId() === null) return;
    this.cancelFlyoutClose();
    this.flyoutCloseTimer = setTimeout(() => this.closeFlyout(), FLYOUT_CLOSE_DELAY_MS);
  }

  protected cancelFlyoutClose(): void {
    if (this.flyoutCloseTimer === null) return;
    clearTimeout(this.flyoutCloseTimer);
    this.flyoutCloseTimer = null;
  }

  protected onGroupKeydown(group: NavGroup, event: KeyboardEvent): void {
    if (this.pinned() || !['ArrowDown', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    this.openFlyout(group, event);
    setTimeout(() => {
      const flyout = document.getElementById(this.flyoutId(group.id));
      flyout?.querySelector<HTMLAnchorElement>('a.nav-link')?.focus();
    });
  }

  protected onFlyoutKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    this.closeFlyout(true);
  }

  protected onNavigationSelected(): void {
    this.query.set('');
    this.searchOpen.set(false);
    this.closeFlyout();
  }

  protected flyoutId(groupId: string): string {
    return `sidebar-flyout-${groupId}`;
  }

  protected groupTriggerId(groupId: string): string {
    return `sidebar-group-${groupId}`;
  }

  private closeFlyout(returnFocus = false): void {
    this.cancelFlyoutClose();
    this.openFlyoutId.set(null);
    if (!returnFocus || this.flyoutTrigger === null) {
      this.flyoutTrigger = null;
      return;
    }
    const trigger = this.flyoutTrigger;
    if (document.activeElement === trigger) return;
    this.suppressNextFlyoutOpen = true;
    trigger.focus({ preventScroll: true });
  }

  private toggleGroup(id: string): void {
    this.openGroups.update(state => ({ ...state, [id]: !state[id] }));
  }

  isActive(item: NavItem): boolean {
    return this.activeRoute() === (item.activePath ?? item.route);
  }

  groupHasActive(g: NavGroup): boolean {
    return g.items.some(it => this.isActive(it));
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

  private readPinnedPreference(): boolean {
    try {
      return typeof localStorage !== 'undefined' && localStorage.getItem(SIDEBAR_PINNED_STORAGE_KEY) === 'true';
    } catch (error) {
      if (error instanceof DOMException) return false;
      throw error;
    }
  }

  private writePinnedPreference(pinned: boolean): void {
    try {
      localStorage.setItem(SIDEBAR_PINNED_STORAGE_KEY, String(pinned));
    } catch (error) {
      if (error instanceof DOMException) return;
      throw error;
    }
  }
}
