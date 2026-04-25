import {
  Component,
  input,
  computed,
  signal,
  ChangeDetectionStrategy,
  HostListener,
  ElementRef,
  inject,
  OnDestroy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { INDICATOR_QUICK_INFO, IndicatorQuickInfo, IndicatorParamDoc } from '../indicators/indicator-reference';

/**
 * Rich indicator tooltip overlay.
 *
 * Uses position: fixed so the panel escapes any overflow: hidden
 * ancestor (e.g. category-card in the indicator catalog).
 *
 * Usage:
 *   <app-indicator-tooltip [indicatorKey]="'ema'" [paramConfigs]="paramArray">
 *     <span class="ind-name">ema</span>
 *   </app-indicator-tooltip>
 */
@Component({
  selector: 'app-indicator-tooltip',
  standalone: true,
  imports: [CommonModule, RouterModule],
  templateUrl: './indicator-tooltip.component.html',
  styleUrls: ['./indicator-tooltip.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class IndicatorTooltipComponent implements OnDestroy {
  private el = inject(ElementRef);

  /** The indicator key, e.g. 'ema', 'bbands', 'macd' */
  indicatorKey = input.required<string>();

  /** Optional fallback description when no doc entry exists */
  fallbackDescription = input<string>('');

  /** Optional override param docs from the backend ParamConfig[] */
  paramConfigs = input<{ name: string; description: string }[]>([]);

  /** Resolved quick-info doc (may be null for undocumented indicators) */
  doc = computed<IndicatorQuickInfo | null>(
    () => INDICATOR_QUICK_INFO[this.indicatorKey()] ?? null
  );

  /** Whether we have rich doc content */
  hasDoc = computed(() => !!this.doc());

  /** Display name: from doc or uppercase the key */
  displayName = computed(() => {
    const d = this.doc();
    if (d) return d.displayName;
    return this.indicatorKey().toUpperCase();
  });

  /** Merged param docs: prefer shared doc, fall back to backend descriptions */
  paramDocs = computed<IndicatorParamDoc[]>(() => {
    const d = this.doc();
    if (d && d.params.length > 0) return d.params;
    return this.paramConfigs().map(p => ({ name: p.name, description: p.description }));
  });

  /** Route fragment for docs page anchor (e.g. 'ind-ema') */
  docsFragment = computed(() => `ind-${this.indicatorKey()}`);

  visible = signal(false);

  /** Fixed positioning (px values for style binding) */
  panelTop = signal<number | null>(null);
  panelBottom = signal<number | null>(null);
  panelLeft = signal(0);
  showAbove = signal(true);

  private hideTimeout: ReturnType<typeof setTimeout> | null = null;
  private showTimeout: ReturnType<typeof setTimeout> | null = null;

  @HostListener('mouseenter')
  onMouseEnter(): void {
    if (this.hideTimeout) {
      clearTimeout(this.hideTimeout);
      this.hideTimeout = null;
    }
    this.showTimeout = setTimeout(() => {
      this.calculateFixedPosition();
      this.visible.set(true);
    }, 200);
  }

  @HostListener('mouseleave')
  onMouseLeave(): void {
    if (this.showTimeout) {
      clearTimeout(this.showTimeout);
      this.showTimeout = null;
    }
    this.hideTimeout = setTimeout(() => {
      this.visible.set(false);
    }, 150);
  }

  ngOnDestroy(): void {
    if (this.hideTimeout) clearTimeout(this.hideTimeout);
    if (this.showTimeout) clearTimeout(this.showTimeout);
  }

  /** Compute fixed position based on trigger's viewport rect */
  private calculateFixedPosition(): void {
    const rect = this.el.nativeElement.getBoundingClientRect();
    const panelWidth = 340;
    const gap = 10;

    // Horizontal: center on trigger, but clamp to viewport
    let left = rect.left + rect.width / 2 - panelWidth / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - panelWidth - 8));
    this.panelLeft.set(left);

    // Vertical: prefer above, fall back to below
    if (rect.top > 300) {
      // Show above
      this.showAbove.set(true);
      this.panelBottom.set(window.innerHeight - rect.top + gap);
      this.panelTop.set(null);
    } else {
      // Show below
      this.showAbove.set(false);
      this.panelTop.set(rect.bottom + gap);
      this.panelBottom.set(null);
    }
  }
}
