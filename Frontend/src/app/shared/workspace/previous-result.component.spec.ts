import { Component, computed, signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { createResultInputTracker } from './previous-result';
import { PreviousResultComponent } from './previous-result.component';

interface StudyInputs {
  readonly symbol: string;
  readonly from: string;
}

/** A host the way a study page uses the pieces: a form, a Run, a shown result. */
@Component({
  imports: [PreviousResultComponent],
  template: `
    <button type="button" (click)="symbol.set('QQQ')">Edit symbol</button>
    <button type="button" (click)="symbol.set('SPY')">Restore symbol</button>
    <button type="button" (click)="from.set('')">Clear start date</button>
    <button type="button" (click)="run()">Run</button>

    <app-previous-result [previous]="tracker.isPrevious()" [captured]="tracker.captured()">
      @if (tracker.captured(); as shown) {
        <p>Result for {{ shown.facts[0].value }}</p>
      }
      <button type="button" resultActions>Use in Deploy</button>
    </app-previous-result>
    <p>{{ tracker.answersCurrentInputs() ? 'Answers the form' : 'Does not answer the form' }}</p>
  `,
})
class StudyHost {
  readonly symbol = signal('SPY');
  readonly from = signal('2026-01-02');
  private readonly inputs = computed<StudyInputs>(() => ({ symbol: this.symbol(), from: this.from() }));

  readonly tracker = createResultInputTracker(this.inputs, (inputs) => ({
    key: `${inputs.symbol}|${inputs.from}`,
    facts: [
      { label: 'Symbol', value: inputs.symbol },
      { label: 'From', value: inputs.from === '' ? 'no date' : inputs.from },
    ],
  }));

  run(): void {
    this.tracker.capture(this.inputs());
  }
}

async function renderStudy() {
  await render(StudyHost);
  return userEvent;
}

describe('previous-result marking', () => {
  it('marks nothing until a result is shown', async () => {
    await renderStudy();

    expect(screen.queryByText('Previous result')).toBeNull();
    expect(screen.getByText('Does not answer the form')).toBeTruthy();
  });

  it('shows a result as the answer to the inputs it was run with', async () => {
    const user = await renderStudy();

    await user.click(screen.getByRole('button', { name: 'Run' }));

    expect(screen.getByText('Result for SPY')).toBeTruthy();
    expect(screen.queryByText('Previous result')).toBeNull();
    expect(screen.getByText('Answers the form')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Use in Deploy' })).toBeTruthy();
  });

  it('marks the result previous with its captured inputs once an input changes, and withdraws what would consume it', async () => {
    const user = await renderStudy();
    await user.click(screen.getByRole('button', { name: 'Run' }));

    await user.click(screen.getByRole('button', { name: 'Edit symbol' }));

    const banner = screen.getByText('Previous result').closest('div');
    expect(banner?.textContent).toContain('SPY');
    expect(banner?.textContent).toContain('2026-01-02');
    // The old result stays readable, but it is no longer an answer to the form.
    expect(screen.getByText('Result for SPY')).toBeTruthy();
    expect(screen.getByText('Does not answer the form')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Use in Deploy' })).toBeNull();
  });

  it('marks the result previous when an edit makes the inputs invalid', async () => {
    const user = await renderStudy();
    await user.click(screen.getByRole('button', { name: 'Run' }));

    await user.click(screen.getByRole('button', { name: 'Clear start date' }));

    expect(screen.getByText('Previous result')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Use in Deploy' })).toBeNull();
  });

  it('stops marking it when the inputs are put back, and when a new run answers them', async () => {
    const user = await renderStudy();
    await user.click(screen.getByRole('button', { name: 'Run' }));
    await user.click(screen.getByRole('button', { name: 'Edit symbol' }));

    await user.click(screen.getByRole('button', { name: 'Restore symbol' }));
    expect(screen.queryByText('Previous result')).toBeNull();
    expect(screen.getByRole('button', { name: 'Use in Deploy' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Edit symbol' }));
    await user.click(screen.getByRole('button', { name: 'Run' }));
    expect(screen.queryByText('Previous result')).toBeNull();
    expect(screen.getByText('Result for QQQ')).toBeTruthy();
  });
});

describe('createResultInputTracker', () => {
  it('captures the inputs a request was submitted with, not the form as it is when the result lands', () => {
    const symbol = signal('SPY');
    const tracker = createResultInputTracker(symbol, (value) => ({
      key: value,
      facts: [{ label: 'Symbol', value }],
    }));

    const submitted = symbol();
    symbol.set('QQQ'); // edited while the run was in flight
    tracker.capture(submitted);

    expect(tracker.captured()?.facts).toEqual([{ label: 'Symbol', value: 'SPY' }]);
    expect(tracker.isPrevious()).toBe(true);
    expect(tracker.answersCurrentInputs()).toBe(false);
  });

  it('forgets the shown result on clear', () => {
    const symbol = signal('SPY');
    const tracker = createResultInputTracker(symbol, (value) => ({ key: value, facts: [] }));
    tracker.capture('SPY');

    tracker.clear();

    expect(tracker.captured()).toBeNull();
    expect(tracker.isPrevious()).toBe(false);
    expect(tracker.answersCurrentInputs()).toBe(false);
  });
});
