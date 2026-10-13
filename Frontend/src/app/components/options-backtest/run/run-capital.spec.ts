import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { sessionOpenMs } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { RunDetailService } from '../run-detail/run-detail.service';
import { inertRunDetail } from '../run-detail/testing/fake-run-detail';
import type { CapitalEvidence, CapitalInvalidEvent, RunFigures } from '../options-backtest.types';
import { expectTradingDatesInNewYork, paragraph, reads } from '../testing/page-actions';
import { capitalEvidence, CASH_LADDER, figures, runDetail } from '../testing/run-fixtures';
import { RunResultComponent } from './run-result.component';

/** The closer look at the trades is not what these specs are about, so it reads nothing. */
const providers = [{ provide: RunDetailService, useValue: inertRunDetail() }];

async function renderResult(figureChanges: Partial<RunFigures> = {}) {
  return render(RunResultComponent, { inputs: { run: runDetail({ figures: figures(figureChanges) }) }, providers });
}

const capital = () => screen.getByRole('region', { name: 'Capital evidence' });
const ladder = () => screen.getByRole('region', { name: 'Cash ladder' });
const tile = (name: string) => within(capital()).getByText(name, { selector: 'dt' }).closest('.tile') as HTMLElement;

const SESSION = '2026-09-17';
const AN_UNFUNDED_CLOSE: CapitalEvidence = capitalEvidence({
  sessions_funded_continuously: 6,
  ended_by: {
    kind: 'unfunded_close',
    session_open_ms: sessionOpenMs(SESSION),
    // 61 minutes after the 09:30 open: 10:31 in New York.
    at_ms: sessionOpenMs(SESSION) + 61 * 60_000,
    free_cash_after_usd: '-180.00',
  },
  least_free_cash_usd: '-180.00',
  closes_relied_on_own_relief: 1,
});

describe('the capital block', () => {
  it('says the cash carried the whole run when nothing ended it, and what the stretch needed, each figure as the server wrote it', async () => {
    await renderResult({ capital_evidence: capitalEvidence() });

    expect(capital().querySelector('.sentence')?.textContent).toBe('Funded continuously through session 10 of 10: the cash carried the whole run.');
    expect(paragraph(/^Nothing ended it/, capital())).toBeTruthy();
    expect(reads(tile('Most collateral held'))).toBe('Most collateral held $500.00 Over every session the replay ran.');
    expect(reads(tile('Cash this path needed'))).toBe(
      'Cash this path needed $380.00 Over the funded stretch only, the steps before the event. True of this recorded path only. It is not a minimum and not a safe amount.',
    );
    expect(reads(tile('Least free cash seen'))).toBe('Least free cash seen $9,620.00 Over every session the replay ran. Below zero is included.');
    expect(reads(tile('Largest assignment obligation held'))).toBe(
      'Largest assignment obligation held $50,000.00 Over every session the replay ran. Sold contracts times strike times multiplier, gross. It is not a loss.',
    );
    expect(tile('Closes that relied on their own relief').querySelector('.big')?.textContent).toBe('0');
  });

  it('says which figure is over the funded stretch and which are over every session the replay ran', async () => {
    await renderResult({ capital_evidence: AN_UNFUNDED_CLOSE });

    const scopeOf = (name: string) => tile(name).querySelector('.scope')?.textContent;
    expect(scopeOf('Cash this path needed')).toBe('Over the funded stretch only, the steps before the event.');
    for (const name of ['Most collateral held', 'Least free cash seen', 'Largest assignment obligation held', 'Closes that relied on their own relief']) {
      expect(scopeOf(name), name).toBe('Over every session the replay ran.');
    }
  });

  it('names an unfunded close with its session and its minute in New York, and the free cash it left', async () => {
    await renderResult({ capital_evidence: AN_UNFUNDED_CLOSE });

    expect(capital().querySelector('.sentence')?.textContent).toBe('Funded continuously through session 6 of 10.');
    expect(reads(paragraph(/^An unfunded close ended it/, capital()))).toBe(
      'An unfunded close ended it: in the session of 2026-09-17 a close at 10:31 ET left free cash at -$180.00, below zero.',
    );
    expect(paragraph(/^Later sessions still ran/, capital())).toBeTruthy();
    expect(reads(tile('Least free cash seen'))).toContain('-$180.00');
    expect(tile('Closes that relied on their own relief').querySelector('.big')?.textContent).toBe('1');
    expectTradingDatesInNewYork(capital());
  });

  it('says a session that ended with a position marked ended it, and names the session', async () => {
    await renderResult({
      capital_evidence: capitalEvidence({
        sessions_funded_continuously: 2,
        ended_by: { kind: 'position_marked', session_open_ms: sessionOpenMs(SESSION), at_ms: null, free_cash_after_usd: null },
      }),
    });

    expect(capital().querySelector('.sentence')?.textContent).toBe('Funded continuously through session 2 of 10.');
    expect(reads(paragraph(/^A position marked and not closed ended it/, capital()))).toBe(
      'A position marked and not closed ended it: the session of 2026-09-17 ended with a position still held, valued at its mark.',
    );
  });

  it('says it was not funded through even the first session, not session 0', async () => {
    await renderResult({
      capital_evidence: capitalEvidence({ sessions_funded_continuously: 0, ended_by: AN_UNFUNDED_CLOSE.ended_by }),
    });

    expect(capital().querySelector('.sentence')?.textContent).toBe('Not funded continuously through even the first of 10 sessions.');
  });

  it('marks the cash this path needed incomplete, and says why, when an entry was refused for cash on the way', async () => {
    await renderResult({ capital_evidence: capitalEvidence({ cash_needed_is_incomplete: true }) });

    const needed = tile('Cash this path needed');
    expect(needed.querySelector('.tag')?.textContent).toBe('incomplete');
    expect(reads(needed)).toContain('a larger starting cash would have traded a different path');
    expect(reads(needed)).toContain('not a minimum and not a safe amount');
  });

  it('says the totals keep every session, so the verdict is not read as a total', async () => {
    await renderResult();

    expect(paragraph(/^The totals above keep every session at its mark/, capital())).toBeTruthy();
  });

  it('states the requirement rule used, the day its page was read in New York, and that it is no evidence of a past requirement', async () => {
    await renderResult();

    const stamp = paragraph(/^Replay rules version 1/, capital());
    expect(reads(stamp)).toBe(
      'Replay rules version 1. Requirement rule: Alpaca’s published rule for covered positions, as its page read on 2026-10-12 . It is today’s rule and is no evidence of what a broker required on a session’s date.',
    );
    expectTradingDatesInNewYork(stamp);
  });

  it('shows a rule it has no words for as an operator-readable label, never as the raw token', async () => {
    await renderResult({ stamps: { replay_rules_version: 2, requirement_rule: 'a_rule_of_the_future', requirement_rule_read_at_ms: 1_791_777_600_000 } });

    const stamp = reads(paragraph(/^Replay rules version 2/, capital()));
    expect(stamp).toContain('Requirement rule: A Rule Of The Future,');
    expect(stamp).not.toContain('a_rule_of_the_future');
  });

  it('shows an event kind it has no words for, in the block and in the ladder, as a label and not as the raw token', async () => {
    // A server newer than the page can send a kind the generated type does not list yet, so the type is widened here.
    const newer = 'a_stop_of_the_future' as CapitalInvalidEvent['kind'];
    const ended = { kind: newer, session_open_ms: sessionOpenMs(SESSION), at_ms: null, free_cash_after_usd: null };
    const evidence = capitalEvidence({ sessions_funded_continuously: 3, ended_by: ended });
    await renderResult({ capital_evidence: evidence, cash_ladder: [{ ...CASH_LADDER[1], capital_evidence: evidence }] });

    expect(reads(paragraph(/^The evidence ended/, capital()))).toBe('The evidence ended: A Stop Of The Future.');
    expect(capital().textContent).not.toContain('a_stop_of_the_future');
    expect(reads(ladder().querySelector('tbody tr'))).toContain('3 of 10 A Stop Of The Future');
    expect(ladder().textContent).not.toContain('a_stop_of_the_future');
  });
});

describe('the cash ladder', () => {
  const rows = () =>
    [...screen.getByRole('table', { name: 'The run replayed at each starting cash' }).querySelectorAll('tbody tr')].map((row) => reads(row));

  it('shows each level as the server wrote it, lowest cash first, the setup’s own marked, and none called safe', async () => {
    await renderResult();

    expect(rows()).toEqual([
      '$5,000.00 0 24 $0.00 10 of 10 nothing $0.00 incomplete',
      '$10,000.00 setup’s own cash 12 2 $1,234.56 10 of 10 nothing $380.00 incomplete',
      '$20,000.00 14 0 $1,500.00 7 of 10 a position marked $500.00',
      '$40,000.00 14 0 $1,500.00 10 of 10 nothing $500.00',
    ]);
    expect(screen.getByText('setup’s own cash').closest('tr')?.classList.contains('own')).toBe(true);
    expect(ladder().textContent).toContain('No level is called safe.');
  });

  it('shows a level too small to enter as entries refused and none filled, beside a larger one that filled them', async () => {
    await renderResult();

    const [smallest, , , largest] = rows();
    expect(smallest).toContain('0 24');
    expect(largest).toContain('14 0');
  });
});

describe('a run saved before capital evidence was kept', () => {
  const NOT_KEPT = { capital_evidence: null, cash_ladder: null, stamps: null } satisfies Partial<RunFigures>;

  it('says "not recorded" in the capital block and the ladder, and keeps every other figure it had', async () => {
    await renderResult(NOT_KEPT);

    expect(reads(paragraph(/^Not recorded\. This run was saved before capital evidence/, capital()))).toBe(
      'Not recorded. This run was saved before capital evidence was kept; its figures are as they were.',
    );
    expect(reads(paragraph(/^Not recorded\. This run was saved before the cash ladder/, ladder()))).toBe(
      'Not recorded. This run was saved before the cash ladder was kept.',
    );
    expect(reads(paragraph(/^The replay rules and the requirement rule/, capital()))).toBe(
      'The replay rules and the requirement rule it used are not recorded.',
    );
    expect(within(capital()).queryByText('Cash this path needed')).toBeNull();
    expect(within(ladder()).queryByRole('table')).toBeNull();
    // The headline and the slippage ladder are as they were.
    expect(reads(screen.getByText('Net profit', { selector: 'dt' }).closest('.tile'))).toBe('Net profit $1,234.56');
    expect(screen.getByRole('table', { name: 'The run replayed at each slippage' })).toBeTruthy();
  });

  it('reads a run whose server sent none of the three keys the same as one that sent them null', async () => {
    const sent: RunFigures = figures();
    delete sent.capital_evidence;
    delete sent.cash_ladder;
    delete sent.stamps;

    await render(RunResultComponent, { inputs: { run: runDetail({ figures: sent }) }, providers });

    expect(capital().textContent).toContain('Not recorded. This run was saved before capital evidence was kept');
    expect(ladder().textContent).toContain('Not recorded. This run was saved before the cash ladder was kept.');
  });
});
