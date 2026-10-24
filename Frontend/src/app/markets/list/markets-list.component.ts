import { ChangeDetectionStrategy, Component, computed, inject, input, signal, untracked } from '@angular/core';
import { Router, RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { SymbolCatalogService } from '../../shared/symbol-catalog/symbol-catalog.service';
import { isHeldRow, type PickerSymbol } from '../../shared/symbol-catalog/symbol-catalog.types';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import { DEFAULT_ADJUSTMENT_MODE } from '../../shared/ticker-catalog';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { WorkspaceHeaderComponent } from '../../shared/workspace/workspace-header.component';
import { WorkspaceStateBlockComponent } from '../../shared/workspace/workspace-state-block.component';
import { menuLabelFor } from '../../shell/app-menu';
import { MARKETS_ROUTE, instrumentCommands, openableTab } from '../instrument-address';

/** Rows drawn at once; the search reaches every other instrument. */
const MAX_ROWS = 200;

/**
 * Markets (`/markets`): the instruments the lake holds, and search over every
 * listed US-equity symbol (PRD #3490 R07, R08).
 *
 * The rows come from the shared joined catalog (`SymbolCatalogService`): lake
 * holdings, with the listing's own status on each. A held instrument is listed
 * whether or not a quote is available; a delisted one stays listed. The page
 * reads that catalog once and starts nothing per row: no fill, no quote, no
 * live subscription.
 *
 * Search is the shared symbol picker, so it carries the catalog's degraded
 * notice and the one coverage gate (ADR 0066): picking a symbol the lake does
 * not hold fills it first, says so, and opens the instrument when the lake
 * confirms the bars. This page builds no gate of its own.
 */
@Component({
  selector: 'app-markets-list',
  imports: [
    RouterLink,
    AssetIdentityComponent,
    SymbolPickerComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './markets-list.component.html',
  styleUrl: './markets-list.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MarketsListComponent {
  private readonly router = inject(Router);
  private readonly view = untracked(() => inject(SymbolCatalogService).viewFor(DEFAULT_ADJUSTMENT_MODE));

  /** The tab a pick opens (`?then=options` and the like); anything this place does not route opens Chart. */
  readonly then = input<string | undefined>(undefined);

  protected readonly title = menuLabelFor(MARKETS_ROUTE);
  protected readonly tab = computed(() => openableTab(this.then()));

  /** What the search holds: empty, because the picker is where a symbol is chosen, not a place that remembers one. */
  protected readonly picked = signal('');

  protected readonly held = computed<readonly PickerSymbol[]>(() => this.view.pool().filter(isHeldRow));
  protected readonly rows = computed(() => this.held().slice(0, MAX_ROWS));
  protected readonly cut = computed(() => Math.max(0, this.held().length - MAX_ROWS));

  /** Why the held list is not on screen, in the catalog's own words; `null` once it is. */
  protected readonly listState = computed<WorkspaceStateContent | null>(() => {
    const status = this.view.status();
    if (status.kind === 'loading') {
      return { kind: 'checking', happened: 'Reading what the lake holds.' };
    }
    if (status.kind === 'unavailable') {
      return {
        kind: 'unavailable',
        happened: 'The instruments the lake holds could not be listed.',
        why: status.message,
        next: 'Read the lake again. Search cannot say what is held until it answers.',
        actions: [{ id: 'retry-lake', label: 'Read the lake again' }],
      };
    }
    if (this.held().length === 0) {
      return {
        kind: 'idle',
        happened: 'The lake holds no instruments yet.',
        next: 'Search for a symbol above. It is filled first, then opens.',
      };
    }
    return null;
  });

  /** The listing catalog's degraded notice: held instruments still list, and search still finds them. */
  protected readonly degraded = computed<WorkspaceStateContent | null>(() => {
    const status = this.view.status();
    return status.kind === 'degraded'
      ? {
          kind: 'partial',
          happened: 'The listing catalog could not be read, so search offers only what the lake holds.',
          why: status.message,
          next: 'Read the listing again to search every listed symbol.',
          actions: [{ id: 'retry-listing', label: 'Read the listing again' }],
        }
      : null;
  });

  protected commandsFor(symbol: string): string[] {
    return instrumentCommands(symbol, this.tab());
  }

  /** A pick, which the picker emits only once the lake holds the instrument. */
  protected onPicked(symbol: string): void {
    this.picked.set(symbol);
    if (symbol !== '') void this.router.navigate(this.commandsFor(symbol));
  }

  protected onAction(id: string): void {
    if (id === 'retry-lake') this.view.reload();
    else if (id === 'retry-listing') this.view.retryVendor();
  }
}
