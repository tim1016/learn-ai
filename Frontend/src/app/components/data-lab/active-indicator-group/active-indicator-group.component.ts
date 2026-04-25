import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Tooltip } from 'primeng/tooltip';
import {
  CATEGORY_META,
  getIndicatorReference,
  IndicatorReferenceEntry,
} from '../../../shared/indicators/indicator-reference';
import {
  ActiveIndicatorEntry,
  ActiveIndicatorParam,
} from '../active-indicator-card/active-indicator-card.component';

export interface IndicatorGroupItem {
  entry: ActiveIndicatorEntry;
  originalIndex: number;
}

@Component({
  selector: 'app-active-indicator-group',
  standalone: true,
  imports: [CommonModule, Tooltip],
  templateUrl: './active-indicator-group.component.html',
  styleUrls: ['./active-indicator-group.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ActiveIndicatorGroupComponent {
  /** Indicator key shared by all items in this group, e.g. 'ema'. */
  name = input.required<string>();
  items = input.required<IndicatorGroupItem[]>();
  paramConfigs = input.required<ActiveIndicatorParam[]>();

  configurePill = output<number>();
  removePill = output<number>();
  /** "Add another" — default fired when the [+ Add] button is clicked. */
  addAnother = output();
  /** Reset every entry in this group to default params. */
  resetGroup = output();
  /** Remove every entry in this group. */
  removeGroup = output();

  protected reference = computed<IndicatorReferenceEntry | null>(() =>
    getIndicatorReference(this.name())
  );

  protected displayName = computed(() => this.reference()?.displayName ?? this.name().toUpperCase());

  protected categoryColor = computed(() => {
    const ref = this.reference();
    return ref ? CATEGORY_META[ref.category].color : 'transparent';
  });

  protected categoryColorSoft = computed(() => {
    const ref = this.reference();
    return ref ? CATEGORY_META[ref.category].colorSoft : 'transparent';
  });

  protected categoryLabel = computed(() => {
    const ref = this.reference();
    return ref ? CATEGORY_META[ref.category].label : '';
  });

  /** Compact param summary per pill, e.g. "length=21" or "fast=12 · slow=26". */
  protected pillLabel(item: IndicatorGroupItem): string {
    const cfgs = this.paramConfigs();
    if (cfgs.length === 1) {
      const p = cfgs[0];
      const v = item.entry.params[p.name];
      return `${p.name}=${this.fmt(v, p.type)}`;
    }
    return cfgs.map((p) => `${p.name}=${this.fmt(item.entry.params[p.name], p.type)}`).join(' · ');
  }

  protected pillModified(item: IndicatorGroupItem): boolean {
    return this.paramConfigs().some((p) => {
      const v = item.entry.params[p.name];
      return typeof v === 'number' && v !== p.default;
    });
  }

  private fmt(v: unknown, type: 'int' | 'float'): string {
    if (typeof v !== 'number') return '–';
    return type === 'float' ? v.toFixed(2).replace(/\.?0+$/, '') : String(v);
  }

  protected onConfigure(idx: number, ev: MouseEvent): void {
    ev.stopPropagation();
    this.configurePill.emit(idx);
  }

  protected onRemove(idx: number, ev: MouseEvent): void {
    ev.stopPropagation();
    this.removePill.emit(idx);
  }

  protected onAdd(): void {
    this.addAnother.emit();
  }

  protected onResetGroup(): void {
    this.resetGroup.emit();
  }

  protected onRemoveGroup(): void {
    this.removeGroup.emit();
  }
}
