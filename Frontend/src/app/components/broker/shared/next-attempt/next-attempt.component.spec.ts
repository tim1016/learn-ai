import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { NextAttemptComponent, type NextAttemptFacts } from './next-attempt.component';

// 2026-09-03 03:59:55 ET: the first send that lands in the pre-market open.
const PRE_MARKET_TRY_MS = 1_788_422_395_000;

/** Renders the component and returns its host element. */
async function renderAttempt(facts: NextAttemptFacts): Promise<HTMLElement> {
  const { fixture } = await render(NextAttemptComponent, { inputs: { facts } });
  return fixture.nativeElement as HTMLElement;
}

describe('NextAttemptComponent (#2440)', () => {
  it('names the time of the next automatic attempt in ET', async () => {
    await renderAttempt({ next_attempt_at_ms: PRE_MARKET_TRY_MS });

    const line = screen.getByText(/Automatic retry/);
    expect(line.textContent).toContain('03:59:55');
    expect(line.textContent).toContain('ET');
    expect(line.textContent).toContain('Automatic retry: allowed from');
    expect(line.textContent).not.toContain('waiting');
  });

  it('shows past eligibility without inferring a waiting state', async () => {
    await renderAttempt({ next_attempt_at_ms: PRE_MARKET_TRY_MS });

    expect(screen.getByText(/Automatic retry/).textContent).toContain('Automatic retry: allowed from');
    expect(screen.getByText(/Automatic retry/).textContent).not.toContain('waiting');
  });

  it('shows a working exit in place of retry eligibility', async () => {
    // The backend drops the time while an exit works; even a stale one would
    // not be shown while an exit is already working.
    await renderAttempt({
      next_attempt_at_ms: PRE_MARKET_TRY_MS,
      exit_working: true,
    });

    expect(
      screen.getByText('An exit is in progress; no automatic attempt is due while it works.'),
    ).toBeTruthy();
    expect(screen.queryByText(/Automatic retry/)).toBeNull();
  });

  it('says the attempt is unknown when the record could not be read', async () => {
    await renderAttempt({ next_attempt_at_ms: null, facts_unreadable: true });

    expect(
      screen.getByText("Automatic retry: eligibility unknown; this notice's record could not be read."),
    ).toBeTruthy();
  });

  it('renders nothing, and takes no space, when nothing is scheduled', async () => {
    const host = await renderAttempt({ next_attempt_at_ms: null });

    expect(host.textContent?.trim()).toBe('');
    expect(host.style.display).toBe('none');
  });

  it('takes its place when there is something to say', async () => {
    const host = await renderAttempt({ exit_working: true });

    expect(host.style.display).toBe('');
  });
});
