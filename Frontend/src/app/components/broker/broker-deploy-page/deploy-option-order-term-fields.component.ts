import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { FormField, form, validate, type FieldState, type FieldTree } from '@angular/forms/signals';

import {
  countProblem,
  percentProblem,
  type OptionCountTerm,
  type OptionOrderTermDefaults,
  type OptionOrderTerms,
  type OptionPercentTerm,
} from './option-structure/option-structure';

/** The four terms, in the order the How step lays them out. */
const TERMS = ['gap_limit_percent', 'entry_wait_seconds', 'cost_limit_percent', 'expiry_close_lead_minutes'] as const;

/** One term's box. A percent is text, so it is kept exactly as typed; a count is a number. */
type TermBox =
  | { readonly percent: true; readonly name: OptionPercentTerm; readonly field: FieldTree<string> }
  | { readonly percent: false; readonly name: OptionCountTerm; readonly field: FieldTree<number | null> };

/** What a box says is wrong with it, once the owner has left it: never per keystroke. */
function shownProblem(field: FieldState<string> | FieldState<number | null>): string | null {
  return field.touched() && field.invalid() ? field.errors()[0]?.message ?? null : null;
}

function asError(message: string | null): { kind: string; message: string } | undefined {
  return message === null ? undefined : { kind: 'option-order-term', message };
}

/**
 * The four option order terms as boxes (#2915), each under the catalogue's
 * label and beside its unit.
 *
 * A percent is kept as typed, so it reaches the server as the owner wrote
 * it. A box that holds what cannot be sent says why under it, in the form's
 * own words, once the owner has left it.
 */
@Component({
  selector: 'app-deploy-option-order-term-fields',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField],
  templateUrl: './deploy-option-order-term-fields.component.html',
  styleUrl: './deploy-option-order-term-fields.component.scss',
})
export class DeployOptionOrderTermFieldsComponent {
  readonly terms = model.required<OptionOrderTerms>();
  /** The catalogue's labels and units. */
  readonly defaults = input.required<OptionOrderTermDefaults>();

  private readonly termsForm = form(this.terms, (terms) => {
    validate(terms.gap_limit_percent, ({ value }) => asError(percentProblem(value(), 'gap_limit_percent')));
    validate(terms.cost_limit_percent, ({ value }) => asError(percentProblem(value(), 'cost_limit_percent')));
    validate(terms.entry_wait_seconds, ({ value }) => asError(countProblem(value())));
    validate(terms.expiry_close_lead_minutes, ({ value }) => asError(countProblem(value())));
  });

  private box(name: (typeof TERMS)[number]): TermBox {
    return name === 'gap_limit_percent' || name === 'cost_limit_percent'
      ? { percent: true, name, field: this.termsForm[name] }
      : { percent: false, name, field: this.termsForm[name] };
  }

  protected readonly boxes = computed(() => {
    const told = this.defaults();
    return TERMS.map((name) => {
      const box = this.box(name);
      return {
        ...box,
        id: `deploy-option-term-${name}`,
        problemId: `deploy-option-term-${name}-problem`,
        label: told[name].label,
        unit: told[name].unit_label,
        problem: shownProblem(box.field()),
      };
    });
  });
}
