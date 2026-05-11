import {
  AfterViewChecked,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  signal,
  viewChildren,
} from '@angular/core';

import {
  IndicatorCategory,
  IndicatorInfo,
} from '../indicator-catalog/indicator-catalog.service';
import { IndicatorPane, paneFor } from './indicator-picker.pane-map';
import { IndicatorPreset, INDICATOR_PRESETS } from './indicator-picker.presets';
import { drawPreview, previewKindFor } from './indicator-picker.preview';

/** Event payload for the (add) output — a single click on an indicator row. */
export interface IndicatorPickerAdd {
  name: string;
  params: Record<string, number>;
}

/** Event payload for hover-preview start/stop. */
export interface IndicatorPickerPreview {
  name: string;
  active: boolean;
}

interface DecoratedIndicator extends IndicatorInfo {
  pane: IndicatorPane;
}

@Component({
  selector: 'app-indicator-picker',
  templateUrl: './indicator-picker.component.html',
  styleUrl: './indicator-picker.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'ip-root' },
})
export class IndicatorPickerComponent implements AfterViewChecked {
  // ── Inputs ──────────────────────────────────────────────────
  readonly categories = input<IndicatorCategory[]>([]);
  /** Names of indicators with one or more active instances. Multiple occurrences
   *  of the same name drive the `+N` badge on that row. */
  readonly activeKeys = input<readonly string[]>([]);
  readonly presets = input<readonly IndicatorPreset[]>(INDICATOR_PRESETS);
  readonly loading = input<boolean>(false);

  // ── Outputs ─────────────────────────────────────────────────
  readonly add = output<IndicatorPickerAdd>();
  readonly addInstance = output<IndicatorPickerAdd>();
  readonly preview = output<IndicatorPickerPreview>();

  // ── State ───────────────────────────────────────────────────
  protected readonly catFilter = signal<ReadonlySet<string>>(new Set());
  protected readonly paneFilter = signal<ReadonlySet<IndicatorPane>>(new Set());
  protected readonly openCats = signal<ReadonlySet<string>>(new Set());
  protected readonly hoveredName = signal<string | null>(null);
  private hoverTimer: ReturnType<typeof setTimeout> | null = null;

  // ── Derived data ────────────────────────────────────────────
  protected readonly allIndicators = computed<DecoratedIndicator[]>(() =>
    this.categories().flatMap(cat =>
      cat.indicators.map(ind => ({ ...ind, pane: paneFor(ind.name) })),
    ),
  );

  protected readonly visibleIndicators = computed<DecoratedIndicator[]>(() => {
    const cats = this.catFilter();
    const panes = this.paneFilter();
    return this.allIndicators().filter(i => {
      if (cats.size && !cats.has(i.category)) return false;
      if (panes.size && !panes.has(i.pane)) return false;
      return true;
    });
  });

  protected readonly totalVisible = computed(() => this.visibleIndicators().length);
  protected readonly totalCatalog = computed(() => this.allIndicators().length);

  /** name -> instance count, derived from the `activeKeys` input. */
  protected readonly activeCount = computed<ReadonlyMap<string, number>>(() => {
    const map = new Map<string, number>();
    for (const key of this.activeKeys()) map.set(key, (map.get(key) ?? 0) + 1);
    return map;
  });

  protected readonly totalStaged = computed(() => this.activeKeys().length);

  protected readonly hasFilter = computed(
    () => this.catFilter().size > 0 || this.paneFilter().size > 0,
  );

  /** Filtered view grouped by category, preserving canonical order. */
  protected readonly groupedView = computed<{ name: string; indicators: DecoratedIndicator[] }[]>(
    () => {
      const visible = this.visibleIndicators();
      const order = this.categories().map(c => c.name);
      const grouped: { name: string; indicators: DecoratedIndicator[] }[] = [];
      for (const name of order) {
        const inds = visible.filter(i => i.category === name);
        if (inds.length) grouped.push({ name, indicators: inds });
      }
      return grouped;
    },
  );

  // ── Hover-preview canvas re-paint ──────────────────────────
  private readonly canvases = viewChildren<ElementRef<HTMLCanvasElement>>('previewCanvas');

  ngAfterViewChecked(): void {
    // Re-draw any preview canvas that's currently rendered. Cheap; canvases
    // only exist while a hover is active and there's at most one.
    for (const ref of this.canvases()) {
      const el = ref.nativeElement;
      const kind = el.dataset['kind'];
      if (kind) drawPreview(el, kind as ReturnType<typeof previewKindFor>);
    }
  }

  // ── Facet toggles ───────────────────────────────────────────
  protected toggleCat(name: string): void {
    const next = new Set(this.catFilter());
    if (next.has(name)) next.delete(name);
    else next.add(name);
    this.catFilter.set(next);
  }

  protected togglePane(pane: IndicatorPane): void {
    const next = new Set(this.paneFilter());
    if (next.has(pane)) next.delete(pane);
    else next.add(pane);
    this.paneFilter.set(next);
  }

  protected clearFilters(): void {
    this.catFilter.set(new Set());
    this.paneFilter.set(new Set());
  }

  protected toggleCatOpen(name: string): void {
    const next = new Set(this.openCats());
    if (next.has(name)) next.delete(name);
    else next.add(name);
    this.openCats.set(next);
  }

  protected isCatOpen(name: string): boolean {
    return this.openCats().has(name);
  }

  protected catColorClass(category: string): string {
    return `ip-cat--${category}`;
  }

  protected paneIcon(pane: IndicatorPane): string {
    return pane === 'overlay' ? '▤' : '▥';
  }

  protected defaultParamsFor(ind: IndicatorInfo): Record<string, number> {
    const out: Record<string, number> = {};
    for (const p of ind.configurable_params) out[p.name] = p.default;
    return out;
  }

  protected activeCountFor(name: string): number {
    return this.activeCount().get(name) ?? 0;
  }

  protected stagedInCat(category: string): number {
    let count = 0;
    for (const ind of this.allIndicators()) {
      if (ind.category !== category) continue;
      count += this.activeCountFor(ind.name);
    }
    return count;
  }

  protected previewKind(ind: DecoratedIndicator): string {
    return previewKindFor(ind.name, ind.pane);
  }

  protected paramSignature(ind: IndicatorInfo): string {
    if (!ind.configurable_params.length) return '()';
    return '(' + ind.configurable_params.map(p => p.name).join(',') + ')';
  }

  // ── Actions ────────────────────────────────────────────────
  protected onAdd(ind: IndicatorInfo): void {
    this.add.emit({ name: ind.name, params: this.defaultParamsFor(ind) });
  }

  protected onAddInstance(ind: IndicatorInfo): void {
    this.addInstance.emit({ name: ind.name, params: this.defaultParamsFor(ind) });
  }

  protected onPreset(preset: IndicatorPreset): void {
    for (const instance of preset.instances) {
      this.addInstance.emit({ name: instance.indicator, params: { ...instance.params } });
    }
  }

  // ── Hover preview ──────────────────────────────────────────
  protected onRowEnter(ind: DecoratedIndicator): void {
    if (this.hoverTimer) clearTimeout(this.hoverTimer);
    this.hoverTimer = setTimeout(() => {
      this.hoveredName.set(ind.name);
      this.preview.emit({ name: ind.name, active: true });
    }, 300);
  }

  protected onRowLeave(ind: DecoratedIndicator): void {
    if (this.hoverTimer) {
      clearTimeout(this.hoverTimer);
      this.hoverTimer = null;
    }
    if (this.hoveredName() === ind.name) {
      this.hoveredName.set(null);
      this.preview.emit({ name: ind.name, active: false });
    }
  }
}
