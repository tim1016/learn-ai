import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal } from '@angular/core';
import { applyEach, form, FormField, max, min } from '@angular/forms/signals';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { distanceInWords, MS_PER_MINUTE } from '../options-backtest/session-time';
import type { SearchKnob, SearchValue } from './options-golden-search.types';

interface Choice {
  selected: boolean;
  label: string;
  shape: 'off' | 'scalar' | 'rule' | 'time';
  originalIndex: number;
  kind: string;
  amount: string;
  count: number;
  anchor: string;
  minutes: number;
}
interface KnobRow {
  path: string;
  vary: boolean;
  values: string;
  priority: number;
  options: Choice[];
}
export interface KnobSelection {
  readonly path: string;
  readonly values: readonly SearchValue[];
  readonly priority: number;
  readonly mode: 'search';
}

export function knobValue(value: SearchValue): string {
  if (value === null) return 'Off';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value !== 'object') return String(value);
  if ((value['anchor'] === 'open' || value['anchor'] === 'close') && typeof value['offset_ms'] === 'number') return distanceInWords({ anchor: value['anchor'], offset_ms: value['offset_ms'] });
  return `On · ${Object.entries(value).map(([key, item]) => `${key.replaceAll('_', ' ')}: ${knobValue(item)}`).join(' · ')}`;
}

function choice(value: SearchValue, originalIndex: number): Choice {
  const object = value !== null && typeof value === 'object' ? value : null;
  const offset = object?.['offset_ms'];
  const count = object?.['count'];
  return { selected: true, label: knobValue(value), shape: value === null ? 'off' : object === null ? 'scalar' : 'anchor' in object ? 'time' : 'rule', originalIndex, kind: String(object?.['kind'] ?? ''), amount: String(object?.['value'] ?? ''), count: typeof count === 'number' ? count : 1, anchor: String(object?.['anchor'] ?? 'close'), minutes: typeof offset === 'number' ? offset / MS_PER_MINUTE : 1 };
}

function selectedValue(option: Choice, knob: SearchKnob): SearchValue {
  if (option.shape === 'off') return null;
  if (option.shape === 'scalar') return knob.legal_values[option.originalIndex];
  if (option.shape === 'time') return { anchor: option.anchor, offset_ms: option.minutes * MS_PER_MINUTE };
  return knob.path.endsWith('reentry_on_stop') ? { kind: option.kind, count: option.count } : { kind: option.kind, value: option.amount };
}

@Component({
  selector: 'app-options-search-knobs',
  imports: [FormField, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-knobs.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchKnobsComponent {
  readonly knobs = input.required<readonly SearchKnob[]>();
  readonly changed = output<readonly KnobSelection[]>();
  protected readonly rows = signal<KnobRow[]>([]);
  protected readonly fields = form(this.rows, (path) => { applyEach(path, (row) => { min(row.priority, 1); max(row.priority, 10); }); });
  protected readonly groups = computed(() => [...new Set(this.knobs().map((knob) => knob.leg_id))].map((id) => ({ id, label: id === null ? 'Shared timing' : `Leg ${id}`, changes: this.knobs().filter((knob, index) => knob.leg_id === id && this.rows()[index]?.vary).length, knobs: this.knobs().map((knob, index) => ({ knob, index })).filter(({ knob }) => knob.leg_id === id) })));
  protected readonly displayValue = knobValue;

  constructor() {
    effect(() => {
      this.rows.set(this.knobs().map((knob) => ({ path: knob.path, vary: false, values: knob.path.endsWith('.offset_ms') && typeof knob.current_value === 'number' ? String(knob.current_value / MS_PER_MINUTE) : knobValue(knob.current_value), priority: 5, options: knob.legal_values.map(choice) })));
    });
    effect(() => {
      const rows = this.rows();
      const knobs = this.knobs();
      this.changed.emit(rows.flatMap((row, index) => {
        if (!row.vary) return [];
        const knob = knobs[index];
        const values: readonly SearchValue[] = knob.value_type === 'category' || knob.value_type === 'boolean'
          ? row.options.filter((option) => option.selected).map((option) => selectedValue(option, knob))
          : row.values.split(',').map((item) => item.trim()).filter((item) => item !== '').map((item) => knob.value_type === 'integer' ? Number(item) * (knob.path.endsWith('.offset_ms') ? MS_PER_MINUTE : 1) : item);
        return [{ path: row.path, values, priority: row.priority, mode: 'search' }];
      }));
    });
  }

  protected kinds(knob: SearchKnob): readonly string[] {
    return knob.path.endsWith('stop_loss') ? ['percent', 'points'] : knob.path.endsWith('momentum') ? ['percent_up', 'percent_down', 'points_up', 'points_down'] : ['at_once', 'on_momentum'];
  }

  protected addOn(index: number): void {
    this.rows.update((rows) => rows.map((row, i) => {
      if (i !== index) return row;
      const source = row.options.find((option) => option.shape === 'rule' || option.shape === 'time');
      return source === undefined ? row : { ...row, options: [...row.options, { ...source, selected: true }] };
    }));
  }
}
