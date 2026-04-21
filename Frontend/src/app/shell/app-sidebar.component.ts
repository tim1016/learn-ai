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
    id: 'stocks',
    title: 'Stocks',
    icon: 'pi pi-chart-line',
    items: [
      { label: 'Market Data', route: '/market-data' },
      { label: 'Tickers', route: '/tickers' },
      { label: 'Technical Analysis', route: '/technical-analysis' },
      { label: 'Stock Analysis', route: '/stock-analysis' },
      { label: 'Snapshots', route: '/snapshots' },
      { label: 'Strategy Lab', route: '/strategy-lab', deprecated: true },
      { label: 'Strategy Validation', route: '/strategy-lab-validation' },
      { label: 'Strategy Docs', route: '/strategy-docs' },
      { label: 'Indicator Validation', route: '/indicator-validation' },
      { label: 'Indicator Docs', route: '/indicator-docs' },
      { label: 'Indicator Report', route: '/indicator-report' },
    ],
  },
  {
    id: 'data-lab',
    title: 'Data Lab',
    icon: 'pi pi-database',
    items: [
      { label: 'Data Lab', route: '/data-lab' },
      { label: 'Indicator Reference', route: '/data-lab-docs' },
      { label: 'Data Quality', route: '/data-quality' },
      { label: 'Pipeline Docs', route: '/data-quality-docs' },
    ],
  },
  {
    id: 'options',
    title: 'Options',
    icon: 'pi pi-objects-column',
    items: [
      { label: 'Options Chain', route: '/options-chain' },
      { label: 'Strategy Builder', route: '/strategy-builder' },
      { label: 'Options Strategy Lab', route: '/options-strategy-lab' },
      { label: 'Options History', route: '/options-history' },
      { label: 'Pricing Lab', route: '/pricing-lab' },
    ],
  },
  {
    id: 'research',
    title: 'Research Lab',
    icon: 'pi pi-search',
    items: [
      { label: 'Research Lab', route: '/research-lab' },
      { label: 'Methodology', route: '/docs/indicator-reliability-methodology' },
    ],
  },
  {
    id: 'portfolio',
    title: 'Portfolio',
    icon: 'pi pi-wallet',
    items: [
      { label: 'Dashboard', route: '/portfolio' },
      { label: 'Engine Lab', route: '/engine' },
      { label: 'Tracked Instruments', route: '/tracked-instruments' },
    ],
  },
];

@Component({
  selector: 'app-sidebar',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, FormsModule, RouterLink],
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

      <div class="status-footer mono">
        <span class="status-dot"></span>
        polygon · live
        <span class="version">v3.4.1</span>
      </div>
    </aside>
  `,
  styles: [`
    :host {
      display: block;
      flex-shrink: 0;
      position: sticky;
      top: 0;
      height: 100vh;
      z-index: 40;
    }

    .sidebar {
      width: 240px;
      height: 100vh;
      background: var(--bg-surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      color: var(--text-secondary);
      font-size: 13px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    .mono {
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }

    // ── Brand ─────────────────────────────────────
    .brand {
      padding: 14px 16px;
      display: flex;
      align-items: center;
      gap: 10px;
      border-bottom: 1px solid var(--border);
    }
    .wordmark {
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: -0.01em;
      color: var(--text-primary);
    }
    .slash {
      color: var(--text-muted);
      font-weight: 500;
    }

    // ── Search ────────────────────────────────────
    .search-wrap {
      position: relative;
      padding: 12px 12px 6px;
    }
    .search-icon {
      position: absolute;
      left: 21px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-muted);
      font-size: 12px;
      pointer-events: none;
    }
    .search-input {
      width: 100%;
      padding: 6px 8px 6px 30px;
      background: var(--bg-canvas);
      border: 1px solid var(--border);
      border-radius: 5px;
      color: var(--text-primary);
      font-family: inherit;
      font-size: 12px;
      outline: none;
      box-sizing: border-box;
      transition: border-color 0.15s;
    }
    .search-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
    }
    .search-kbd {
      position: absolute;
      right: 18px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 10px;
      color: var(--text-muted);
      border: 1px solid var(--border);
      padding: 1px 5px;
      border-radius: 3px;
      pointer-events: none;
    }

    // ── Nav scroll ────────────────────────────────
    .nav-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 6px 6px 16px;

      &::-webkit-scrollbar { width: 6px; }
      &::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    }

    // ── Groups ────────────────────────────────────
    .nav-group {
      margin-bottom: 2px;
    }
    .nav-group-header {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 10px;
      background: transparent;
      border: 0;
      border-radius: 4px;
      color: var(--text-secondary);
      font-family: inherit;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      cursor: pointer;
      text-align: left;
      transition: background 0.12s, color 0.12s;
    }
    .nav-group-header:hover {
      background: var(--bg-elevated);
    }
    .nav-group-header.has-active {
      color: var(--text-primary);
    }
    .group-icon {
      font-size: 12px;
      color: var(--text-muted);
    }
    .nav-group-header.has-active .group-icon {
      color: var(--accent);
    }
    .group-title {
      flex: 1;
    }
    .chevron {
      font-size: 9px;
      color: var(--text-muted);
      transition: transform 0.12s ease;
    }
    .chevron.open {
      transform: rotate(90deg);
    }

    .nav-group-items {
      padding: 2px 0 4px;
    }

    // ── Links ─────────────────────────────────────
    .nav-link {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px 6px 28px;
      text-decoration: none;
      color: var(--text-secondary);
      background: transparent;
      border-left: 2px solid transparent;
      margin-left: -2px;
      font-size: 13px;
      font-weight: 400;
      border-radius: 0 3px 3px 0;
      cursor: pointer;
      transition: background 0.1s, color 0.1s;
    }
    .nav-link:hover {
      background: var(--bg-elevated);
      color: var(--text-primary);
    }
    .nav-link.active {
      color: var(--text-primary);
      background: var(--bg-hover);
      border-left-color: var(--accent);
      font-weight: 500;
    }
    .nav-link-label {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .nav-link-group {
      font-size: 10px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .nav-link-badge {
      font-size: 9px;
      padding: 1px 4px;
      border-radius: 3px;
      background: rgba(245, 158, 11, 0.18);
      color: var(--warn);
      letter-spacing: 0.04em;
    }
    .flat-matches {
      padding: 2px 0;
    }
    .empty {
      padding: 12px;
      font-size: 12px;
      color: var(--text-muted);
      text-align: center;
    }

    // ── Status footer ─────────────────────────────
    .status-footer {
      padding: 10px 16px 12px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: var(--text-muted);
    }
    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--bull);
      box-shadow: 0 0 6px var(--bull);
      flex-shrink: 0;
    }
    .version {
      margin-left: auto;
    }
  `],
})
export class AppSidebarComponent {
  private router = inject(Router);
  private destroyRef = inject(DestroyRef);

  readonly groups = NAV;

  /** Current route URL — updated on NavigationEnd. Signal so child bindings refresh. */
  private currentUrl = signal<string>(this.router.url);

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
    const url = this.currentUrl();
    // Exact match, or URL starts with route + '/' (so /research-lab/signal-report/:id highlights Research Lab).
    return url === route || url.startsWith(route + '/');
  }

  groupHasActive(g: NavGroup): boolean {
    return g.items.some(it => this.isActive(it.route));
  }

  private groupContainsUrl(g: NavGroup, url: string): boolean {
    return g.items.some(it => url === it.route || url.startsWith(it.route + '/'));
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
