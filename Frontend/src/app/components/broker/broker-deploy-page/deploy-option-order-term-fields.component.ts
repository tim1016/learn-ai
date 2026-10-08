import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';
import { FormField, form, validate, type FieldState, type FieldTree } from '@angular/forms/signals';

import {
  ORDER_TERMS,
  termProblem,
  type OptionCountTerm,
  type OptionOrderTermDefaults,
  type OptionOrderTerms,
  type OptionPercentTerm,
} from './option-structure/option-structure';

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
 * it. What a box may hold is the catalogue's range for its term: a box that
 * holds anything else says so under it, in the server's words for the range
 * and with the server's reason for it when it gives one, once the owner has
 * left it. The form holds no bound of its own.
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
  /** The catalogue's labels, units and ranges. */
  readonly defaults = input.required<OptionOrderTermDefaults>();

  private readonly termsForm = form(this.terms, (terms) => {
    const refused = (name: (typeof ORDER_TERMS)[number], value: string | number | null) =>
      asError(termProblem(value, this.defaults()[name].bounds));
    validate(terms.gap_limit_percent, ({ value }) => refused('gap_limit_percent', value()));
    validate(terms.entry_wait_seconds, ({ value }) => refused('entry_wait_seconds', value()));
    validate(terms.cost_limit_percent, ({ value }) => refused('cost_limit_percent', value()));
    validate(terms.expiry_close_lead_minutes, ({ value }) => refused('expiry_close_lead_minutes', value()));
  });

  private box(name: (typeof ORDER_TERMS)[number]): TermBox {
    return name === 'gap_limit_percent' || name === 'cost_limit_percent'
      ? { percent: true, name, field: this.termsForm[name] }
      : { percent: false, name, field: this.termsForm[name] };
  }

  protected readonly boxes = computed(() => {
    const told = this.defaults();
    return ORDER_TERMS.map((name) => {
      const box = this.box(name);
      return {
        ...box,
        id: `deploy-option-term-${name}`,
        problemId: `deploy-option-term-${name}-problem`,
        label: told[name].label,
        unit: told[name].unit_label,
        problem: shownProblem(box.field()),
        /** The server's reason for the range, said with a refusal. */
        rangeReason: told[name].range_reason,
      };
    });
  });
}
