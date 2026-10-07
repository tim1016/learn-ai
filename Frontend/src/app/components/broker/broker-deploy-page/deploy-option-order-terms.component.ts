import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import {
  isPercent,
  type OptionOrderTermDefaults,
  type OptionOrderTerms,
} from './option-structure/option-structure';

/** The four terms, in the order the How step lays them out. */
const TERMS = ['gap_limit_percent', 'entry_wait_seconds', 'cost_limit_percent', 'expiry_close_lead_minutes'] as const;
type TermName = (typeof TERMS)[number];

function isPercentTerm(name: TermName): name is 'gap_limit_percent' | 'cost_limit_percent' {
  return name === 'gap_limit_percent' || name === 'cost_limit_percent';
}

/**
 * Deploy's option order terms (#2915), which stand where the stock exit
 * terms stand for a bot that trades options: the widest bid-ask gap a leg
 * may have, how long an entry waits, the most opening and closing at once
 * may cost, and how long before the close an expiry-day position is closed.
 *
 * Each term's label, unit and starting value are the catalogue's. A percent
 * is kept as typed, so it reaches the server as the owner wrote it. What a
 * term may be is the server's to judge; a box only has to hold a number.
 */
@Component({
  selector: 'app-deploy-option-order-terms',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-option-order-terms.component.html',
  styleUrl: './deploy-option-order-terms.component.scss',
})
export class DeployOptionOrderTermsComponent {
  /** The owner's terms; `null` until the catalogue's starting terms have been read. */
  readonly terms = input.required<OptionOrderTerms | null>();
  /** The catalogue's labels, units and starting values; `null` until it has been read. */
  readonly defaults = input.required<OptionOrderTermDefaults | null>();

  readonly termsChange = output<OptionOrderTerms>();

  protected readonly fields = computed(() => {
    const told = this.defaults();
    const held = this.terms();
    if (told === null || held === null) return null;
    return TERMS.map((name) => {
      const value = held[name];
      return {
        name,
        id: `deploy-option-term-${name}`,
        label: told[name].label,
        unit: told[name].unit_label,
        value: value ?? '',
        invalid: isPercentTerm(name) ? !isPercent(held[name]) : value === null,
      };
    });
  });

  protected set(name: TermName, event: Event): void {
    const held = this.terms();
    if (held === null || !(event.target instanceof HTMLInputElement)) return;
    const text = event.target.value.trim();
    if (isPercentTerm(name)) {
      this.termsChange.emit({ ...held, [name]: text });
      return;
    }
    const count = Number(text);
    this.termsChange.emit({ ...held, [name]: text !== '' && Number.isInteger(count) && count >= 0 ? count : null });
  }
}
