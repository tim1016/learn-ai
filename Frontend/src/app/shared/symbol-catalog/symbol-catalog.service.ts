import { Injectable, computed, inject, type Signal } from '@angular/core';

import { TickerCatalogService } from '../ticker-catalog';
import type { PriceAdjustmentMode } from '../data-lake';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  VendorCatalogService,
  LAKE_BACKFILLABLE_ASSET_CLASS,
  type VendorSymbolEntry,
} from './vendor-catalog.service';
import type { PickerSymbol } from './symbol-catalog.types';

/**
 * The shared symbol picker's universe: every listed US symbol, annotated
 * with what the lake actually holds (ADR — symbol picker, 2026-09-20).
 *
 * Membership used to be the lake itself: the picker offered only symbols the
 * lake held, and anything else was a dead end that sent the operator to the
 * Observatory. The card now owns an ensure-coverage gate, so "not held" is
 * no longer unselectable — picking it backfills first — and the honest menu
 * becomes the vendor's listing universe with the lake's coverage badge on
 * each row. The lake stays the only thing anything *reads*; this service
 * only decides what the menu offers.
 *
 * The join is held-first: symbols the lake holds keep the lake's ranking
 * (deeply backfilled above sparsely held), vendor-only actives follow
 * alphabetically. Delisted vendor rows are deliberately *not* offered —
 * they cannot be traded and can only enter the lake through the backfill
 * panel's explicit "include delisted" toggle, which keeps survivorship-bias
 * universes an operator's choice rather than a default. A delisted symbol
 * the lake already holds stays pickable (its bars are real), badged as
 * delisted.
 */
export interface SymbolCatalogView {
  readonly pool: Signal<readonly PickerSymbol[]>;
  readonly recent: Signal<readonly string[]>;
  /** One exhaustive availability verdict; contradictory flag sets cannot exist. */
  readonly status: Signal<SymbolCatalogStatus>;
  /**
   * The verdict as the three booleans a picker card actually renders, so
   * hosts stop re-deriving them: the loading note, the lake-dark banner and
   * the vendor-degraded banner are one mapping, not one per host.
   */
  readonly loading: Signal<boolean>;
  readonly unavailableMessage: Signal<string | null>;
  readonly degradedMessage: Signal<string | null>;
  /**
   * Refresh the lake's coverage — the read a dropdown-open means by
   * "reload". The vendor catalog is deliberately untouched: it is read
   * once per tab and shared by every picker, so an ordinary open costs no
   * vendor traffic.
   */
  readonly reload: () => void;
  /** Re-fetch the vendor catalog — only the degraded banner's Retry. */
  readonly retryVendor: () => void;
}

export type SymbolCatalogStatus =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready' }
  | { readonly kind: 'degraded'; readonly message: string }
  | { readonly kind: 'unavailable'; readonly message: string };

/** The vendor rows a picker offers: US-equity actives only. */
export function offerableVendorRows(
  entries: readonly VendorSymbolEntry[],
): readonly VendorSymbolEntry[] {
  return entries.filter(
    (entry) => entry.asset_class === LAKE_BACKFILLABLE_ASSET_CLASS && entry.status === 'active',
  );
}

/**
 * Lake holdings (ranked) joined with vendor status, then vendor-only
 * actives appended alphabetically. Pure so the join contract is testable
 * without Angular.
 */
export function joinCatalog(
  lakePool: readonly TickerOption[],
  vendorEntries: readonly VendorSymbolEntry[] | null,
): readonly PickerSymbol[] {
  if (vendorEntries === null) {
    return lakePool.map((option) => ({ ...option, delisted: false }));
  }

  // The lake pool re-joins on every catalog reload, so the vendor lookup
  // is a Map — a linear .find per lake row multiplied by backfill reloads.
  const vendorBySymbol = new Map(vendorEntries.map((entry) => [entry.symbol, entry]));
  const lakeRows: readonly PickerSymbol[] = lakePool.flatMap((option) => {
    const vendor = vendorBySymbol.get(option.symbol);
    // A lake row the vendor classifies outside the backfillable universe
    // (a legacy import, say) can never be covered by the gate — it must
    // not be offered just because bars exist.
    if (vendor && vendor.asset_class !== LAKE_BACKFILLABLE_ASSET_CLASS) return [];
    return [{ ...option, delisted: vendor?.status === 'inactive' }];
  });

  const held = new Set(lakePool.map((option) => option.symbol));
  const vendorOnly = offerableVendorRows(vendorEntries)
    .filter((entry) => !held.has(entry.symbol))
    .map(toVendorOnlyRow)
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
  return [...lakeRows, ...vendorOnly];
}

function toVendorOnlyRow(entry: VendorSymbolEntry): PickerSymbol {
  return {
    symbol: entry.symbol,
    // Same fallback rule the lake pool uses: a row is never blank just
    // because nobody named it.
    name: entry.name?.trim() || entry.symbol,
    exchange: entry.exchange ?? undefined,
    firstHeld: null,
    lastHeld: null,
    delisted: false,
  };
}

/**
 * The vendor's delisted US-equity rows — the symbols no joined view offers
 * by default. The backfill panel appends them only behind its explicit
 * "include delisted" toggle, which keeps a survivorship-biased universe an
 * operator's choice (ADR 0066, decision 4).
 */
export function delistedVendorRows(entries: readonly VendorSymbolEntry[]): readonly PickerSymbol[] {
  return entries
    .filter(
      (entry) => entry.asset_class === LAKE_BACKFILLABLE_ASSET_CLASS && entry.status === 'inactive',
    )
    .map((entry) => ({
      symbol: entry.symbol,
      name: entry.name?.trim() || entry.symbol,
      exchange: entry.exchange ?? undefined,
      firstHeld: null,
      lastHeld: null,
      delisted: true,
    }))
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
}

@Injectable({ providedIn: 'root' })
export class SymbolCatalogService {
  private readonly vendor = inject(VendorCatalogService);
  private readonly lake = inject(TickerCatalogService);
  private readonly views = new Map<PriceAdjustmentMode, SymbolCatalogView>();

  /**
   * One joined view per adjustment mode, sharing the lake's per-mode
   * resource exactly as {@link TickerCatalogService.viewFor} does. Creating
   * the view installs a resource, so callers must step outside a reactive
   * context (`untracked`) exactly as they do for the lake view — same
   * NG0602 constraint, same reason.
   */
  viewFor(mode: PriceAdjustmentMode): SymbolCatalogView {
    const existing = this.views.get(mode);
    if (existing) return existing;

    const lakeView = this.lake.viewFor(mode);

    const status = computed<SymbolCatalogStatus>(() => {
      const lakeReason = lakeView.unavailable();
      if (lakeReason !== null) return { kind: 'unavailable', message: lakeReason };
      if (lakeView.loading()) return { kind: 'loading' };
      const vendorReason = this.vendor.unavailable();
      if (vendorReason !== null) return { kind: 'degraded', message: vendorReason };
      if (this.vendor.loading()) return { kind: 'loading' };
      return { kind: 'ready' };
    });
    const view: SymbolCatalogView = {
      pool: computed(() => joinCatalog(lakeView.pool(), this.vendor.entries())),
      recent: lakeView.recent,
      status,
      loading: computed(() => status().kind === 'loading'),
      unavailableMessage: computed(() => {
        const verdict = status();
        return verdict.kind === 'unavailable' ? verdict.message : null;
      }),
      degradedMessage: computed(() => {
        const verdict = status();
        return verdict.kind === 'degraded' ? verdict.message : null;
      }),
      reload: () => {
        lakeView.reload();
      },
      retryVendor: () => {
        this.vendor.reload();
      },
    };
    this.views.set(mode, view);
    return view;
  }
}
