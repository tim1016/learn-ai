import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { type CdkDragDrop, DragDropModule, moveItemInArray } from '@angular/cdk/drag-drop';

import { BotTileComponent } from '../bot-tile/bot-tile.component';
import type { ChartBar, ChartFillMarker, GalleryBotView } from '../lib/gallery.types';
import {
  GALLERY_PAGE_SIZE,
  autoDivision,
  loadLayout,
  paginate,
  resetLayout,
  saveLayout,
  type TileLayout,
} from '../lib/gallery-layout';

const MIN_SPAN = 1;

interface ResizeSession {
  readonly sid: string;
  readonly pointerId: number;
  readonly startColSpan: number;
  readonly startRowSpan: number;
  readonly startClientX: number;
  readonly startClientY: number;
  readonly cellWidth: number;
  readonly cellHeight: number;
}

/**
 * Reorderable, resizable, paginated wall of `BotTileComponent`s for the
 * broker-v2 bot gallery.
 *
 * Default arrangement is a near-square auto grid (`autoDivision`, row-major
 * catalog order), overridable per tile by a persisted `TileLayout`. Reorder
 * uses Angular CDK (`cdkDropList` with `cdkDropListOrientation="mixed"` for
 * 2D grid reordering, `moveItemInArray` on drop); drag is restricted to a
 * small grip (`cdkDragHandle`) so it never competes with the tile's own
 * click-to-navigate body or quick-action button. Resize has no CDK
 * primitive, so it's a custom corner handle driven by raw pointer events
 * (`pointerdown` on the handle, `pointermove`/`pointerup` on `document` via
 * host bindings, mirroring `BotTileComponent`'s `document:keydown.escape`
 * pattern), snapping to whole grid cells and clamped to `[1, cols]` /
 * `[1, rows]`.
 *
 * Order + spans persist per account via `gallery-layout.ts`. A roster over
 * `GALLERY_PAGE_SIZE` paginates; reorder/resize act on the current page only
 * — page-relative indices are translated to the full-list offset before
 * mutating so a drop on page 2 doesn't corrupt page 1's order.
 */
@Component({
  selector: 'app-bot-gallery-dock',
  imports: [DragDropModule, BotTileComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:pointermove)': 'onResizePointerMove($event)',
    '(document:pointerup)': 'onResizePointerEnd($event)',
    '(document:pointercancel)': 'onResizePointerEnd($event)',
  },
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

  readonly action = output<{ sid: string; actionId: string }>();

  private readonly galleryGrid = viewChild<ElementRef<HTMLDivElement>>('galleryGrid');

  /** Raw page-navigation state. The roster can shrink independent of any explicit navigation, so nothing reads this directly — see `page` below. */
  private readonly pageState = signal(0);

  private readonly persistedLayout = signal<readonly TileLayout[]>([]);
  private loadedAccountId: string | null = null;
  private resizeSession: ResizeSession | null = null;

  protected readonly grid = computed(() => autoDivision(this.bots().length));

  /** Persisted order/spans synced to the current roster: dropped sids fall away, new sids append at 1x1 in catalog order. */
  protected readonly effectiveLayout = computed<readonly TileLayout[]>(() => {
    const bots = this.bots();
    const knownSids = new Set(bots.map((bot) => bot.sid));
    const persisted = this.persistedLayout().filter((tile) => knownSids.has(tile.sid));
    const persistedSids = new Set(persisted.map((tile) => tile.sid));
    const appended = bots
      .filter((bot) => !persistedSids.has(bot.sid))
      .map((bot): TileLayout => ({ sid: bot.sid, colSpan: 1, rowSpan: 1 }));
    return [...persisted, ...appended];
  });

  private readonly botsBySid = computed(() => new Map(this.bots().map((bot) => [bot.sid, bot])));

  /** `effectiveLayout` keyed by sid — every span lookup below is per-tile inside a `@for`, so this avoids an O(n) `find` per tile. */
  private readonly effectiveLayoutBySid = computed(
    () => new Map(this.effectiveLayout().map((tile) => [tile.sid, tile])),
  );

  // `pages` only depends on the item count, not on which page was
  // requested — `page: 0` here is an arbitrary valid argument, not a
  // meaningful "current page". Computed independent of `pageState`/`page`
  // so `page`'s own clamp below (which reads `pageCount`) can't cycle back
  // into itself.
  protected readonly pageCount = computed(() => paginate(this.effectiveLayout(), 0).pages);

  /**
   * `pageState` clamped to `[0, pageCount)`. The roster can shrink (bots
   * leaving) independent of any explicit `goToPage` navigation, so every
   * consumer of "the current page" — the footer text, the Next/Previous
   * disabled state, and the page-relative index math in `onDropped` /
   * `onResizePointerStart` — must read through this, never `pageState`
   * directly, or a stale out-of-range page desyncs the footer ("page 2 of
   * 1") and can misalign a drop/resize against the wrong slice.
   */
  protected readonly page = computed(() => Math.max(0, Math.min(this.pageState(), this.pageCount() - 1)));

  private readonly pagedLayout = computed(() => paginate(this.effectiveLayout(), this.page()));

  protected readonly pageTiles = computed(() => this.pagedLayout().pageItems);

  /** The current page's bots, in tile order — `pageTiles` filtered through the live roster (a sid can outlive its bot for one tick during a delta). */
  protected readonly pageBots = computed<GalleryBotView[]>(() => {
    const bySid = this.botsBySid();
    const bots: GalleryBotView[] = [];
    for (const tile of this.pageTiles()) {
      const bot = bySid.get(tile.sid);
      if (bot) bots.push(bot);
    }
    return bots;
  });

  constructor() {
    // Reload the persisted layout only when the account identity actually
    // changes — a same-account signal churn (new bars/markers ticking in)
    // must not clobber an in-session reorder/resize with a stale re-read.
    effect(() => {
      const accountId = this.accountId();
      if (this.loadedAccountId === accountId) return;
      this.loadedAccountId = accountId;
      this.persistedLayout.set(loadLayout(accountId));
      this.pageState.set(0);
    });
  }

  protected colSpanFor(sid: string): number {
    return this.effectiveLayoutBySid().get(sid)?.colSpan ?? MIN_SPAN;
  }

  protected rowSpanFor(sid: string): number {
    return this.effectiveLayoutBySid().get(sid)?.rowSpan ?? MIN_SPAN;
  }

  protected forwardAction(event: { sid: string; actionId: string }): void {
    this.action.emit(event);
  }

  protected onDropped(event: CdkDragDrop<readonly TileLayout[]>): void {
    if (event.previousIndex === event.currentIndex) return;
    // `previousIndex`/`currentIndex` are positions within the rendered
    // `pageBots` DOM order. `pageTiles` (and the `effectiveLayout` offset
    // math below) can diverge from `pageBots` for one tick during a live
    // roster delta (a sid present in the persisted layout with no matching
    // bot yet) — bail rather than apply a move computed against a
    // different-length array than what the user actually saw.
    if (this.pageBots().length !== this.pageTiles().length) return;
    const pageStart = this.page() * GALLERY_PAGE_SIZE;
    const next = [...this.effectiveLayout()];
    moveItemInArray(next, pageStart + event.previousIndex, pageStart + event.currentIndex);
    this.persist(next);
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

  protected onResizePointerStart(event: PointerEvent, sid: string): void {
    // Left button only for mouse; touch/pen have no `button` semantics.
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const tile = this.effectiveLayoutBySid().get(sid);
    const gridEl = this.galleryGrid()?.nativeElement;
    if (!tile || !gridEl) return;
    const { cols } = this.grid();
    const rowsOnPage = Math.max(1, Math.ceil(this.pageTiles().length / Math.max(cols, 1)));
    this.resizeSession = {
      sid,
      pointerId: event.pointerId,
      startColSpan: tile.colSpan,
      startRowSpan: tile.rowSpan,
      startClientX: event.clientX,
      startClientY: event.clientY,
      cellWidth: gridEl.clientWidth / Math.max(cols, 1) || 1,
      cellHeight: gridEl.clientHeight / rowsOnPage || 1,
    };
  }

  protected onResizePointerMove(event: PointerEvent): void {
    const session = this.resizeSession;
    if (!session || event.pointerId !== session.pointerId) return;
    const deltaCols = Math.round((event.clientX - session.startClientX) / session.cellWidth);
    const deltaRows = Math.round((event.clientY - session.startClientY) / session.cellHeight);
    this.applyResize(session.sid, session.startColSpan + deltaCols, session.startRowSpan + deltaRows);
  }

  protected onResizePointerEnd(event: PointerEvent): void {
    if (!this.resizeSession || event.pointerId !== this.resizeSession.pointerId) return;
    this.resizeSession = null;
  }

  /**
   * Clamps to `[1, cols]` / `[1, rows]` and persists. Kept as its own
   * method (rather than inlined into the pointermove handler) so the
   * span-persistence behavior is directly unit-testable without
   * synthesizing a full pointer-drag sequence through jsdom.
   */
  protected applyResize(sid: string, colSpan: number, rowSpan: number): void {
    const { cols, rows } = this.grid();
    const clampedCol = Math.min(Math.max(colSpan, MIN_SPAN), Math.max(cols, MIN_SPAN));
    const clampedRow = Math.min(Math.max(rowSpan, MIN_SPAN), Math.max(rows, MIN_SPAN));
    const existing = this.effectiveLayoutBySid().get(sid);
    if (!existing || (existing.colSpan === clampedCol && existing.rowSpan === clampedRow)) return;
    const next = this.effectiveLayout().map((tile): TileLayout =>
      tile.sid === sid ? { ...tile, colSpan: clampedCol, rowSpan: clampedRow } : tile);
    this.persist(next);
  }

  private persist(layout: readonly TileLayout[]): void {
    this.persistedLayout.set(layout);
    saveLayout(this.accountId(), layout);
  }
}
