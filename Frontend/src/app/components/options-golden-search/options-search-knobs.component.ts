import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal } from '@angular/core';
import { applyEach, form, max, min } from '@angular/forms/signals';

import type { ServedSetup } from '../options-backtest/options-backtest.types';
import type { SearchValue } from './options-golden-search.types';
import { newRule, ruleSummary, ruleValues, RULE_LABELS, type RuleDraft, type RuleKey } from './options-search-rule';
import { OptionsSearchRuleComponent } from './options-search-rule.component';

export interface KnobSelection {
  readonly path: string;
  readonly values: readonly SearchValue[];
  readonly priority: number;
  readonly mode: 'search';
}
const LEG_RULES = ['stop_loss', 'momentum', 'reentry_on_stop'] as const;

@Component({
  selector: 'app-options-search-knobs',
  imports: [OptionsSearchRuleComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-knobs.component.html',
  styleUrl: './options-search-knobs.component.scss',
})
export class OptionsSearchKnobsComponent {
  readonly setup = input.required<ServedSetup>();
  readonly changed = output<readonly KnobSelection[] | null>();
  protected readonly scope = signal<'all' | 'individual'>('all');
  protected readonly active = signal(0);
  protected readonly advanced = signal(false);
  private readonly customized = signal(false);
  protected readonly model = signal<{ all: RuleDraft[]; legs: RuleDraft[][]; cutoff: RuleDraft }>({ all: [], legs: [], cutoff: newRule('no_reentry_after', null) });
  protected readonly fields = form(this.model, (path) => {
    applyEach(path.all, (rule) => { min(rule.priority, 1); max(rule.priority, 10); });
    applyEach(path.legs, (leg) => applyEach(leg, (rule) => { min(rule.priority, 1); max(rule.priority, 10); }));
    min(path.cutoff.priority, 1); max(path.cutoff.priority, 10);
  });
  protected readonly rows = computed(() => this.scope() === 'all' ? this.model().all : this.model().legs[this.active()] ?? []);
  protected readonly summary = computed(() => this.rows().some((row) => row.vary) ? 'Exploring ' + this.rows().filter((row) => row.vary).map((row) => RULE_LABELS[row.key].toLowerCase()).join(', ') : 'All rules stay as saved');
  protected readonly selection = computed<readonly KnobSelection[] | null>(() => {
    const model = this.model();
    const selections: KnobSelection[] = [];
    const append = (row: RuleDraft, path: string, shared: boolean): boolean => {
      if (!row.vary) return true;
      if (!Number.isInteger(row.priority) || row.priority < 1 || row.priority > 10) return false;
      const values = ruleValues(row);
      if (values === null) return false;
      selections.push({ path, values: shared ? values.map((value) => Object.fromEntries(this.setup().legs.map((leg) => [leg.leg_id, value]))) : values, priority: row.priority, mode: 'search' });
      return true;
    };
    if (this.scope() === 'all') {
      if (!model.all.every((row) => append(row, `all_legs.${row.key}`, true))) return null;
    } else {
      for (const [index, rows] of model.legs.entries()) if (!rows.every((row) => append(row, `legs.${this.setup().legs[index].leg_id}.${row.key}`, false))) return null;
    }
    return append(model.cutoff, 'no_reentry_after', false) ? selections : null;
  });

  constructor() {
    effect(() => {
      const setup = this.setup();
      const legs = setup.legs.map((leg) => LEG_RULES.map((key) => newRule(key, leg[key] ?? null)));
      this.model.set({ all: legs[0]?.map((row) => ({ ...row })) ?? [], legs, cutoff: newRule('no_reentry_after', setup.no_reentry_after ?? null) });
      this.scope.set('all'); this.active.set(0); this.customized.set(false);
    });
    effect(() => this.changed.emit(this.selection()));
  }

  protected saved(key: RuleKey): string {
    if (key === 'no_reentry_after') return this.cutoffSummary();
    if (this.scope() === 'individual') return ruleSummary(key, this.setup().legs[this.active()][key] ?? null);
    const values = this.setup().legs.map((leg) => ruleSummary(key, leg[key] ?? null));
    return new Set(values).size === 1 ? values[0] : this.setup().legs.map((leg, index) => `${leg.leg_id}: ${values[index]}`).join(' · ');
  }
  protected legLabel(index: number): string {
    const leg = this.setup().legs[index];
    return `${leg.side === 'sell' ? 'Sell' : 'Buy'} ${leg.contracts} ${leg.strike.kind === 'atm' ? 'ATM' : `ATM offset ${leg.strike.steps}`} ${leg.right} (${leg.leg_id})`;
  }
  protected customize(): void {
    if (this.scope() === 'individual') return;
    if (!this.customized()) this.model.update((model) => ({ ...model, legs: model.legs.map((rows) => rows.map((row, index) => model.all[index].vary ? { ...model.all[index] } : row)) }));
    this.customized.set(true);
    this.scope.set('individual');
  }
  protected together(): void { this.scope.set('all'); }
  protected readonly cutoffSummary = computed(() => ruleSummary('no_reentry_after', this.setup().no_reentry_after ?? null));
}
