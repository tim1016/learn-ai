import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import type { SearchValue } from './options-golden-search.types';
import { RULE_KINDS, RULE_LABELS, ruleSummary, ruleValues, type RuleDraft } from './options-search-rule';

@Component({
  selector: 'app-options-search-rule',
  imports: [FormField],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-rule.component.html',
  styleUrl: './options-search-knobs.component.scss',
})
export class OptionsSearchRuleComponent {
  readonly field = input.required<FieldTree<RuleDraft>>();
  readonly saved = input.required<string>();
  readonly priority = input(false);
  protected readonly draft = computed(() => this.field()().value());
  protected readonly label = computed(() => RULE_LABELS[this.draft().key]);
  protected readonly kinds = computed(() => RULE_KINDS[this.draft().key]);
  protected readonly values = computed(() => ruleValues(this.draft()));
  protected choiceLabel(value: SearchValue): string {
    if (value === null) return 'Off';
    const full = ruleSummary(this.draft().key, value);
    return this.draft().key === 'stop_loss' ? full.replace(' of option premium', '') : this.draft().key === 'reentry_on_stop' ? full.split(',')[0] : full;
  }
  protected mode(vary: boolean): void { this.field().vary().value.set(vary); }
}
