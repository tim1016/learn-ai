import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { RunDetail } from '../options-backtest.types';

/** Material interpretation limits stay visible independently of the evidence disclosures. */
@Component({
  selector: 'app-options-backtest-run-limitations',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-limitations.component.html',
  styleUrl: './run-limitations.component.scss',
})
export class RunLimitationsComponent {
  readonly run = input.required<RunDetail>();
  readonly inspect = output<'capital' | 'coverage' | 'risk'>();
  protected readonly facts = computed(() => {
    const f = this.run().figures;
    if (!f) return [];
    const capital = f.capital_evidence;
    const facts: { text: string; section: 'capital' | 'coverage' | 'risk' }[] = [];
    if (!capital) facts.push({ text: 'Capital evidence not recorded', section: 'capital' });
    else if (capital.ended_by) facts.push({ text: 'Capital evidence ended before the full path', section: 'capital' });
    else if (capital.cash_needed_is_incomplete) facts.push({ text: 'Funding amount incomplete', section: 'capital' });
    if (f.entries_refused_for_cash) facts.push({ text: `${f.entries_refused_for_cash} ${f.entries_refused_for_cash === 1 ? 'entry' : 'entries'} refused for cash`, section: 'capital' });
    if (f.sessions_incomplete) facts.push({ text: `${f.sessions_incomplete} ${f.sessions_incomplete === 1 ? 'session' : 'sessions'} ended with marked positions`, section: 'coverage' });
    if (f.sessions_skipped) facts.push({ text: `${f.sessions_skipped} skipped ${f.sessions_skipped === 1 ? 'session' : 'sessions'}`, section: 'coverage' });
    if (f.entries_dropped) facts.push({ text: `${f.entries_dropped} ${f.entries_dropped === 1 ? 'entry' : 'entries'} lacked fill bars`, section: 'coverage' });
    if (f.legs_without_a_starting_price) facts.push({ text: `${f.legs_without_a_starting_price} ${f.legs_without_a_starting_price === 1 ? 'leg' : 'legs'} lacked a starting price`, section: 'coverage' });
    if (f.dashboard?.risk_unavailable_reason) facts.push({ text: f.dashboard.risk_unavailable_reason, section: 'risk' });
    if (f.dashboard?.annualization_unavailable_reason) facts.push({ text: f.dashboard.annualization_unavailable_reason, section: 'risk' });
    if (!f.dashboard) facts.push({ text: 'Daily statistical dashboard not recorded', section: 'risk' });
    return facts;
  });
}
