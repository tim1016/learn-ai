import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { type CdkDragDrop, DragDropModule, moveItemInArray } from '@angular/cdk/drag-drop';

import { BotTileComponent } from '../bot-tile/bot-tile.component';
import type { ChartBar, ChartFillMarker, GalleryBotView, GalleryLiveStatus } from '../lib/gallery.types';
import {
  GALLERY_PAGE_SIZE,
  GALLERY_TILE_HEADER_HEIGHT_PX,
  chooseColumns,
  loadLayout,
  paginate,
  resetLayout,
  saveLayout,
  spliceVisibleIntoFullOrder,
  type TileLayout,
} from '../lib/gallery-layout';

/** Matches `--space-3` in `_tokens.scss`, the gap `.gallery-dock__grid` renders between cells — kept in sync by hand since `chooseColumns` needs a px number, not a CSS custom property. */
const GALLERY_GAP_PX = 12;
/** Assumed grid area before the first `ResizeObserver` measurement lands — a reasonable widescreen default so the very first paint (and any environment where the observer never fires, e.g. tests) still gets a sane column count instead of a degenerate one from an unmeasured 0x0 element. */
const DEFAULT_GRID_WIDTH_PX = 1600;
const DEFAULT_GRID_HEIGHT_PX = 900;

export type GalleryStatusFilter = 'all' | 'running' | 'needs_attention' | 'stopped';

interface FilterOption {
  readonly value: GalleryStatusFilter;
  readonly label: string;
}

const FILTER_OPTIONS: readonly FilterOption[] = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'needs_attention', label: 'Needs attn' },
  { value: 'stopped', label: 'Stopped' },
];

const STATUS_LABEL: Record<GalleryLiveStatus, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  stale: 'Delayed',
  error: 'Feed error',
};

/** Single-select status predicate for the footer filter (design spec §5, D7). */
function matchesStatusFilter(bot: GalleryBotView, filter: GalleryStatusFilter): boolean {
  switch (filter) {
    case 'all': return true;
    case 'running': return bot.running;
    case 'needs_attention': return bot.running && bot.needs_attention;
    case 'stopped': return !bot.running;
  }
}

/**
 * Reorderable, paginated, filterable wall of `BotTileComponent`s for the
 * broker-v2 bot gallery.
 *
 * Layout is a uniform, aspect-ratio-optimised `flex-wrap` grid
 * (`chooseColumns` — see `lib/gallery-layout.ts`): tiles are `flex: 1 1
 * <one-column-basis>` so a short trailing row grows to fill the full width
 * instead of leaving a dead cell. The column/row division is recomputed off
 * the grid container's live measured size (`ResizeObserver`, re-attached
 * whenever the `@if`-gated grid element itself changes identity — e.g. the
 * status filter toggling the "no matches" note in and out).
 *
 * Reorder uses Angular CDK (`cdkDropList` with `cdkDropListOrientation="mixed"`
 * for 2D wrap reordering, `moveItemInArray` on drop); drag is restricted to
 * a small grip (`cdkDragHandle`) so it never competes with the tile's own
 * click-to-navigate body or quick-action button.
 *
 * The footer owns a single-select status filter (`All` / `Running` /
 * `Needs attn` / `Stopped`, design spec §5, D7) that runs *before*
 * pagination/`chooseColumns` — everything downstream (page count, column
 * choice) operates on the filtered sid set. Persisted order, though, is
 * reconciled against the FULL unfiltered roster (`fullOrder`): dropped sids
 * fall away, new sids append at the end in catalog order. `effectiveLayout`
 * is a filtered VIEW onto `fullOrder`, never a replacement for it — a bot
 * the active filter is hiding keeps its place in `fullOrder` even while
 * invisible, and reordering the visible subset splices back into
 * `fullOrder` (`spliceVisibleIntoFullOrder`) rather than overwriting it, so
 * a drag-drop while filtered can't silently destroy a hidden bot's position
 * (see `onDropped`). It also renders the connection-status `●Live`
 * indicator, driven by the `status` input the page forwards from
 * `GalleryLiveStore`.
 *
 * Order persists per account via `gallery-layout.ts` as a flat sid array
 * (no per-tile spans — resize was removed, design spec §4/D5). A roster
 * over `GALLERY_PAGE_SIZE` paginates; reorder acts on the current page only
 * — page-relative indices are translated to the full-list offset before
 * mutating so a drop on page 2 doesn't corrupt page 1's order.
 *
 * KNOWN LIMITATION: the drag handle is pointer/touch-only — it has no
 * keyboard equivalent yet, so it's marked `tabindex="-1" aria-hidden="true"`
 * rather than advertising a focusable control that Enter/Space silently
 * does nothing on. The always-available catalog order is the working
 * fallback. Real keyboard reorder is a follow-up (design spec §11).
 */
@Component({
  selector: 'app-bot-gallery-dock',
  imports: [DragDropModule, BotTileComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './bot-gallery-dock.component.html',
  styleUrl: './bot-gallery-dock.component.scss',
})
export class BotGalleryDockComponent {
  readonly bots = input.required<GalleryBotView[]>();
  readonly barsBySymbol = input.required<ReadonlyMap<string, readonly ChartBar[]>>();
  readonly markersBySid = input.required<ReadonlyMap<string, readonly ChartFillMarker[]>>();
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  /** Sids with a confirmed quick action in flight — forwarded to each tile's `pending` input (mirrors `bots-roster`'s `pendingBotIds`). */
  readonly pendingSids = input<ReadonlySet<string>>(new Set());
  /** The account's live-feed connection status, forwarded from `GalleryLiveStore` via the page — drives the footer's `●Live` indicator. */
  readonly status = input.required<GalleryLiveStatus>();

  readonly action = output<{ sid: string; actionId: string }>();

  private readonly galleryGrid = viewChild<ElementRef<HTMLDivElement>>('galleryGrid');
  private readonly destroyRef = inject(DestroyRef);

  protected readonly filterOptions = FILTER_OPTIONS;
  protected readonly filter = signal<GalleryStatusFilter>('all');

  /** Raw page-navigation state. The roster/filter can shrink independent of any explicit navigation, so nothing reads this directly — see `page` below. */
  private readonly pageState = signal(0);

  private readonly persistedLayout = signal<TileLayout>([]);
  private loadedAccountId: string | null = null;

  /** The grid container's last measured size — updated by `ResizeObserver`; seeded with a widescreen default so the first paint (and any host that never fires the observer, e.g. tests) still gets a sane column count. */
  private readonly gridSize = signal<{ width: number; height: number }>({
    width: DEFAULT_GRID_WIDTH_PX,
    height: DEFAULT_GRID_HEIGHT_PX,
  });

  protected readonly statusLabel = computed(() => STATUS_LABEL[this.status()]);

  /** Live per-bucket counts against the *unfiltered* roster, so switching filters doesn't make the other segments' counts move. */
  protected readonly filterCounts = computed<Record<GalleryStatusFilter, number>>(() => {
    const bots = this.bots();
    return {
      all: bots.length,
      running: bots.filter((bot) => bot.running).length,
      needs_attention: bots.filter((bot) => bot.running && bot.needs_attention).length,
      stopped: bots.filter((bot) => !bot.running).length,
    };
  });

  protected readonly filteredBots = computed<GalleryBotView[]>(() => {
    const filter = this.filter();
    return this.bots().filter((bot) => matchesStatusFilter(bot, filter));
  });

  /** Grid division for the *current page* — a >20-bot roster's page 2 has far fewer tiles than the full roster, so dividing by the full count would give it the wrong column count. */
  protected readonly grid = computed(() => chooseColumns(
    this.pageTiles().length,
    this.gridSize().width,
    this.gridSize().height,
    GALLERY_GAP_PX,
    GALLERY_TILE_HEADER_HEIGHT_PX,
  ));

  /**
   * Persisted order synced to the *full, unfiltered* roster: dropped sids
   * fall away, new sids append in catalog order. This is the canonical
   * order — `effectiveLayout` below is a filtered VIEW onto it, never a
   * replacement for it, so a bot the active status filter is hiding keeps
   * its place here even while invisible (see `onDropped`).
   */
  private readonly fullOrder = computed<TileLayout>(() => {
    const bots = this.bots();
    const knownSids = new Set(bots.map((bot) => bot.sid));
    const persisted = this.persistedLayout().filter((sid) => knownSids.has(sid));
    const persistedSids = new Set(persisted);
    const appended = bots.filter((bot) => !persistedSids.has(bot.sid)).map((bot) => bot.sid);
    return [...persisted, ...appended];
  });

  /** `fullOrder` filtered down to the sids passing the active status filter, preserving their relative order — what's actually paginated/rendered. */
  protected readonly effectiveLayout = computed<TileLayout>(() => {
    const visibleSids = new Set(this.filteredBots().map((bot) => bot.sid));
    return this.fullOrder().filter((sid) => visibleSids.has(sid));
  });

  private readonly botsBySid = computed(() => new Map(this.bots().map((bot) => [bot.sid, bot])));

  // `pages` only depends on the item count, not on which page was
  // requested — `page: 0` here is an arbitrary valid argument, not a
  // meaningful "current page". Computed independent of `pageState`/`page`
  // so `page`'s own clamp below (which reads `pageCount`) can't cycle back
  // into itself.
  protected readonly pageCount = computed(() => paginate(this.effectiveLayout(), 0).pages);

  /**
   * `pageState` clamped to `[0, pageCount)`. The filtered roster can shrink
   * (bots leaving, or a filter narrowing) independent of any explicit
   * `goToPage` navigation, so every consumer of "the current page" — the
   * footer text, the Next/Previous disabled state, and the page-relative
   * index math in `onDropped` — must read through this, never `pageState`
   * directly, or a stale out-of-range page desyncs the footer ("page 2 of
   * 1") and can misalign a drop against the wrong slice.
   */
  protected readonly page = computed(() => Math.max(0, Math.min(this.pageState(), this.pageCount() - 1)));

  private readonly pagedLayout = computed(() => paginate(this.effectiveLayout(), this.page()));

  protected readonly pageTiles = computed(() => this.pagedLayout().pageItems);

  /** The current page's bots, in tile order — `pageTiles` filtered through the live roster (a sid can outlive its bot for one tick during a delta). */
  protected readonly pageBots = computed<GalleryBotView[]>(() => {
    const bySid = this.botsBySid();
    const bots: GalleryBotView[] = [];
    for (const sid of this.pageTiles()) {
      const bot = bySid.get(sid);
      if (bot) bots.push(bot);
    }
    return bots;
  });

  constructor() {
    // Reload the persisted layout only when the account identity actually
    // changes — a same-account signal churn (new bars/markers ticking in)
    // must not clobber an in-session reorder with a stale re-read.
    effect(() => {
      const accountId = this.accountId();
      if (this.loadedAccountId === accountId) return;
      this.loadedAccountId = accountId;
      this.persistedLayout.set(loadLayout(accountId));
      this.pageState.set(0);
    });

    // Track the grid container's real size so `chooseColumns` scores
    // against the actual viewport rather than only the seeded default.
    // Re-attached (not bound once) because the grid element is `@if`-gated
    // behind the filter's "no matches" note and gets a new identity each
    // time it reappears.
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      this.gridSize.set({ width: rect.width, height: rect.height });
    });
    this.destroyRef.onDestroy(() => observer.disconnect());
    effect(() => {
      const gridEl = this.galleryGrid()?.nativeElement;
      observer.disconnect();
      if (gridEl) observer.observe(gridEl);
    });
  }

  protected onFilterChange(next: GalleryStatusFilter): void {
    if (this.filter() === next) return;
    this.filter.set(next);
    this.pageState.set(0);
  }

  protected forwardAction(event: { sid: string; actionId: string }): void {
    this.action.emit(event);
  }

  protected onDropped(event: CdkDragDrop<TileLayout>): void {
    if (event.previousIndex === event.currentIndex) return;
    // `previousIndex`/`currentIndex` are positions within the rendered
    // `pageBots` DOM order. `pageTiles` (and the `effectiveLayout` offset
    // math below) can diverge from `pageBots` for one tick during a live
    // roster delta (a sid present in the persisted layout with no matching
    // bot yet) — bail rather than apply a move computed against a
    // different-length array than what the user actually saw.
    if (this.pageBots().length !== this.pageTiles().length) return;
    const pageStart = this.page() * GALLERY_PAGE_SIZE;
    const reorderedVisible = [...this.effectiveLayout()];
    moveItemInArray(reorderedVisible, pageStart + event.previousIndex, pageStart + event.currentIndex);
    // Persist against the FULL order, not the filtered `effectiveLayout` —
    // persisting the filtered list directly would drop every sid the active
    // filter is hiding (it's not in `effectiveLayout` to begin with), and
    // the next read would re-append them at the end in catalog order,
    // silently destroying whatever position they held.
    this.persist(spliceVisibleIntoFullOrder(this.fullOrder(), reorderedVisible));
  }

  protected goToPage(delta: number): void {
    const next = Math.min(Math.max(this.page() + delta, 0), this.pageCount() - 1);
    this.pageState.set(next);
  }

  protected onResetLayout(): void {
    resetLayout(this.accountId());
    this.persistedLayout.set([]);
    this.pageState.set(0);
  }

  private persist(layout: TileLayout): void {
    this.persistedLayout.set(layout);
    saveLayout(this.accountId(), layout);
  }
}
