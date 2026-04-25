import { ChangeDetectionStrategy, Component, HostListener, computed, effect, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { DataLabDocsCardComponent, IndicatorDoc, IndicatorTab } from './data-lab-docs-card/data-lab-docs-card.component';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { INDICATOR_REFERENCE_LIST } from '../../../shared/indicators/indicator-reference';

type PanelFilter = 'all' | 'overlay' | 'sub';

@Component({
  selector: 'app-data-lab-docs',
  standalone: true,
  imports: [CommonModule, RouterModule, DataLabDocsCardComponent, PageHeaderComponent],
  templateUrl: './data-lab-docs.component.html',
  styleUrls: ['./data-lab-docs.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabDocsComponent {
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  protected query = signal('');
  protected panelFilter = signal<PanelFilter>('all');
  protected activeId = signal<string>('ema');
  protected activeTab = signal<IndicatorTab>('explain');

  private fragment = toSignal(this.route.fragment);

  constructor() {
    effect(() => {
      const f = this.fragment();
      if (!f) return;
      const m = /^ind-([a-z0-9_-]+)(?:\/(explain|math|use))?$/i.exec(f);
      if (!m) return;
      const id = m[1].toLowerCase();
      const tab = (m[2] as IndicatorTab) ?? 'explain';
      if (this.allIndicators.some(i => i.name === id)) {
        this.activeId.set(id);
        this.activeTab.set(tab);
      }
    });

    effect(() => {
      const id = this.activeId();
      const tab = this.activeTab();
      const currentFragment = this.fragment();
      const desired = tab === 'explain' ? `ind-${id}` : `ind-${id}/${tab}`;
      if (currentFragment !== desired) {
        this.router.navigate([], {
          relativeTo: this.route,
          fragment: desired,
          replaceUrl: true,
        });
      }
    });
  }

  protected filteredIndicators = computed<IndicatorDoc[]>(() => {
    const q = this.query().trim().toLowerCase();
    const panel = this.panelFilter();
    return this.allIndicators.filter(ind => {
      if (panel === 'overlay' && ind.panelType !== 'overlay') return false;
      if (panel === 'sub' && ind.panelType !== 'sub-panel') return false;
      if (q === '') return true;
      const hay = `${ind.name} ${ind.displayName} ${ind.description}`.toLowerCase();
      return hay.includes(q);
    });
  });

  protected activeIndicator = computed<IndicatorDoc | undefined>(() =>
    this.allIndicators.find(i => i.name === this.activeId())
  );

  protected activeIndexInFilter = computed(() => {
    const list = this.filteredIndicators();
    return list.findIndex(i => i.name === this.activeId());
  });

  protected prevIndicator = computed<IndicatorDoc | undefined>(() => {
    const list = this.filteredIndicators();
    const idx = this.activeIndexInFilter();
    return idx > 0 ? list[idx - 1] : undefined;
  });

  protected nextIndicator = computed<IndicatorDoc | undefined>(() => {
    const list = this.filteredIndicators();
    const idx = this.activeIndexInFilter();
    return idx >= 0 && idx < list.length - 1 ? list[idx + 1] : undefined;
  });

  protected hasDelayFlag(ind: IndicatorDoc): boolean {
    const s = `${ind.timeframeBehavior} ${ind.dataNotes.join(' ')}`.toLowerCase();
    return /15[- ]?minute delay|real[- ]?time|scalp/.test(s);
  }

  protected hasCriticalCaveat(ind: IndicatorDoc): boolean {
    return ind.dataNotes.some(n =>
      /volume-dependent|session boundary|drift heavily|extended hours/i.test(n)
    );
  }

  protected selectIndicator(name: string): void {
    this.activeId.set(name);
  }

  protected onRelatedClick(name: string): void {
    if (this.allIndicators.some(i => i.name === name)) {
      this.activeId.set(name);
    }
  }

  protected setPanelFilter(p: PanelFilter): void {
    this.panelFilter.set(p);
  }

  protected onSearchInput(event: Event): void {
    const val = (event.target as HTMLInputElement).value;
    this.query.set(val);
  }

  protected clearSearch(): void {
    this.query.set('');
  }

  protected goPrev(): void {
    const p = this.prevIndicator();
    if (p) this.activeId.set(p.name);
  }

  protected goNext(): void {
    const n = this.nextIndicator();
    if (n) this.activeId.set(n.name);
  }

  @HostListener('document:keydown', ['$event'])
  onKeydown(ev: KeyboardEvent): void {
    const target = ev.target as HTMLElement | null;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.key === 'ArrowLeft') { this.goPrev(); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight') { this.goNext(); ev.preventDefault(); }
  }

  allIndicators: IndicatorDoc[] = INDICATOR_REFERENCE_LIST.map((e) => ({ ...e, name: e.key }));

  csvBaseColumns = [
    { column: 'unix_ts', type: 'int', description: 'Unix timestamp in milliseconds (UTC)' },
    { column: 'iso_time', type: 'string', description: 'ISO 8601 datetime string (UTC)' },
    { column: 'open', type: 'float', description: 'Opening price of the minute bar' },
    { column: 'high', type: 'float', description: 'Highest price during the minute bar' },
    { column: 'low', type: 'float', description: 'Lowest price during the minute bar' },
    { column: 'close', type: 'float', description: 'Closing price of the minute bar' },
    { column: 'volume', type: 'float', description: 'Shares traded during the minute bar' },
    { column: 'vwap', type: 'float', description: 'Volume-weighted average price' },
    { column: 'transactions', type: 'int', description: 'Number of transactions' },
  ];

  validationNotes = [
    'All float values are rounded to 6 decimal places for consistency',
    'Empty cells represent NaN — indicator warm-up period or insufficient data',
    'Timestamps represent the start of each 1-minute bar (bar-open convention)',
    'Data is de-duplicated by timestamp and sorted chronologically',
    'Polygon returns consolidated tape data (not exchange-specific)',
    'Date range is chunked into ~111-day windows to stay within 50,000-bar API limit',
    'The metadata JSON file describes every column, its source, library, and parameters',
  ];

  dataCaveats = [
    { icon: 'pi-clock', label: 'Warmup Bars', text: 'Most indicators require `length` bars before producing valid values. Earlier rows will be NaN.' },
    { icon: 'pi-exclamation-triangle', label: 'Missing Bars', text: 'EMA-like indicators drift if missing candles exist in the data.' },
    { icon: 'pi-replay', label: 'Session Reset', text: 'VWAP must reset at session boundary (daily). Multi-day VWAP is not standard.' },
    { icon: 'pi-chart-bar', label: 'Volume Dependency', text: 'VWAP, AD, CMF, MFI, OBV require reliable volume data. Zero-volume bars make these indicators unreliable.' },
    { icon: 'pi-sync', label: 'Resampling', text: 'Ensure OHLCV resample logic is consistent with TradingView (especially volume aggregation).' },
    { icon: 'pi-moon', label: 'RTH vs Extended', text: 'Volume-based indicators behave very differently in extended hours due to thin volume.' },
  ];
}
