import {
  ChangeDetectionStrategy,
  Component,
  computed,
  ElementRef,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Tooltip } from 'primeng/tooltip';
import { IndicatorTooltipComponent } from '../indicator-tooltip/indicator-tooltip.component';
import {
  IndicatorCatalogService,
  IndicatorCategory,
  IndicatorInfo,
} from './indicator-catalog.service';

/**
 * Shared indicator-catalog browser. Mirrors data-lab's left-column catalog
 * UI so the research-lab feature- and signal-runner pages reuse the same
 * visual treatment. Single-select mode replaces the active selection on
 * click; multi-select is a toggle (matches data-lab's stacking behavior).
 */
@Component({
  selector: 'app-indicator-catalog',
  standalone: true,
  imports: [CommonModule, FormsModule, Tooltip, IndicatorTooltipComponent],
  templateUrl: './indicator-catalog.component.html',
  styleUrls: ['./indicator-catalog.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorCatalogComponent {
  private catalog = inject(IndicatorCatalogService);

  /** 'single' replaces the active selection, 'multi' toggles. */
  selectionMode = input<'single' | 'multi'>('multi');

  /** Names of indicators currently selected — drives checkbox state and the
   *  per-category "N selected" pill. Caller owns this state. */
  selectedNames = input<ReadonlySet<string>>(new Set<string>());

  /** Optional override for the eyebrow line (defaults to "Indicator Catalog"). */
  eyebrow = input<string>('Indicator Catalog');

  /** Optional secondary hint shown next to the eyebrow. */
  hint = input<string>('All calculations use pandas-ta');

  /** Fired when the user clicks an indicator. The host decides what to do
   *  with the click given selectionMode + current selection. */
  indicatorSelected = output<IndicatorInfo>();

  /** Fired when the user clicks the (i) preview button on a catalog row. */
  previewRequested = output<string>();

  /** Fired when the user clicks the "+" button on a configurable indicator
   *  to add another instance with different params (multi-select only). */
  addInstanceRequested = output<IndicatorInfo>();

  protected readonly categories = this.catalog.categories;
  protected readonly loading = this.catalog.loading;

  protected readonly catalogQuery = signal<string>('');
  protected readonly expandedCategories = signal<Set<string>>(new Set());

  protected readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');

  protected readonly filteredCategories = computed<IndicatorCategory[]>(() => {
    const q = this.catalogQuery().trim().toLowerCase();
    if (!q) return this.categories();
    return this.categories()
      .map((c) => ({
        ...c,
        indicators: c.indicators.filter(
          (i) =>
            i.name.toLowerCase().includes(q) ||
            i.description.toLowerCase().includes(q),
        ),
      }))
      .filter((c) => c.indicators.length > 0);
  });

  constructor() {
    this.catalog.load();
  }

  protected toggleCategory(catName: string): void {
    this.expandedCategories.update((set) => {
      const next = new Set(set);
      if (next.has(catName)) next.delete(catName);
      else next.add(catName);
      return next;
    });
  }

  protected isCategoryExpanded(catName: string): boolean {
    return this.expandedCategories().has(catName);
  }

  protected isSelected(name: string): boolean {
    return this.selectedNames().has(name);
  }

  protected categorySelectedCount(catName: string): number {
    const cat = this.categories().find((c) => c.name === catName);
    if (!cat) return 0;
    const names = this.selectedNames();
    return cat.indicators.filter((i) => names.has(i.name)).length;
  }

  protected onIndicatorClick(ind: IndicatorInfo): void {
    this.indicatorSelected.emit(ind);
  }

  protected onPreviewClick(name: string, ev: MouseEvent): void {
    ev.stopPropagation();
    this.previewRequested.emit(name);
  }

  protected onAddInstanceClick(ind: IndicatorInfo, ev: MouseEvent): void {
    ev.stopPropagation();
    this.addInstanceRequested.emit(ind);
  }

  /** Expose focus() so a parent ("Add indicator" button) can jump focus to
   *  the catalog search box. */
  focusSearch(): void {
    this.searchInput()?.nativeElement.focus();
  }
}
