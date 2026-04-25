import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Tooltip } from 'primeng/tooltip';
import {
  CATEGORY_META,
  getIndicatorReference,
  IndicatorReferenceEntry,
} from '../../../shared/indicators/indicator-reference';

export interface ActiveIndicatorParam {
  name: string;
  type: 'int' | 'float';
  default: number;
  min: number;
  max: number;
  description: string;
}

export interface ActiveIndicatorEntry {
  name: string;
  params: Record<string, number>;
}

@Component({
  selector: 'app-active-indicator-card',
  standalone: true,
  imports: [CommonModule, Tooltip],
  templateUrl: './active-indicator-card.component.html',
  styleUrls: ['./active-indicator-card.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ActiveIndicatorCardComponent {
  entry = input.required<ActiveIndicatorEntry>();
  paramConfigs = input.required<ActiveIndicatorParam[]>();
  /** Optional warning shown next to the title (e.g. "Not recommended on 1m"). */
  timeframeWarning = input<string>('');

  configure = output();
  remove = output();
  resetDefaults = output();

  protected reference = computed<IndicatorReferenceEntry | null>(() =>
    getIndicatorReference(this.entry().name)
  );

  protected displayName = computed(() => {
    const ref = this.reference();
    return ref ? ref.displayName : this.entry().name.toUpperCase();
  });

  protected categoryLabel = computed(() => {
    const ref = this.reference();
    if (!ref) return '';
    return CATEGORY_META[ref.category].label;
  });

  protected categoryColor = computed(() => {
    const ref = this.reference();
    if (!ref) return 'transparent';
    return CATEGORY_META[ref.category].color;
  });

  protected categoryColorSoft = computed(() => {
    const ref = this.reference();
    if (!ref) return 'transparent';
    return CATEGORY_META[ref.category].colorSoft;
  });

  protected panelLabel = computed(() => {
    const ref = this.reference();
    if (!ref) return '';
    return ref.panelType === 'overlay' ? 'Overlay' : 'Sub-panel';
  });

  /** Compact param summary; `modified` is true when the value differs from
   *  the default surfaced by INDICATOR_CONFIGS (the configurable_params
   *  passed in via [paramConfigs]). */
  protected paramChips = computed<{ label: string; modified: boolean }[]>(() => {
    const e = this.entry();
    return this.paramConfigs().map((p) => {
      const v = e.params[p.name];
      const display = typeof v === 'number'
        ? (p.type === 'float' ? v.toFixed(2).replace(/\.?0+$/, '') : String(v))
        : '–';
      const modified = typeof v === 'number' && v !== p.default;
      return { label: `${p.name}=${display}`, modified };
    });
  });

  /** True when any param diverges from defaults — drives the rail dot. */
  protected hasModifiedParams = computed(() =>
    this.paramChips().some((c) => c.modified)
  );

  protected onConfigure(ev: MouseEvent): void {
    ev.stopPropagation();
    this.configure.emit();
  }

  protected onRemove(ev: MouseEvent): void {
    ev.stopPropagation();
    this.remove.emit();
  }

  protected onReset(ev: MouseEvent): void {
    ev.stopPropagation();
    this.resetDefaults.emit();
  }
}
