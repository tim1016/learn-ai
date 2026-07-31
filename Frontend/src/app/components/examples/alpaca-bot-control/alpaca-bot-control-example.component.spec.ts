import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { AlpacaBotControlExampleComponent } from './alpaca-bot-control-example.component';

describe('AlpacaBotControlExampleComponent', () => {
  it('renders the committed static scenario gallery without a loading state', async () => {
    await render(AlpacaBotControlExampleComponent);

    expect(screen.getByText('Static diagnostic example')).toBeTruthy();
    expect(screen.getByText('Static product example — no broker actions or production API calls.')).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Account state unprovable' })).toBeTruthy();
    expect(screen.getByText('Watching for two consecutive green one-minute bars.')).toBeTruthy();
  });

  it('uses the selected committed envelope for both diagnostic lenses', async () => {
    await render(AlpacaBotControlExampleComponent);

    const selector = screen.getByLabelText('Review scenario');
    fireEvent.change(selector, { target: { value: 'stopped_carryover_mismatch' } });
    expect(screen.getByText('Resume blocked')).toBeTruthy();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    expect(screen.getByText('Resume custody proof')).toBeTruthy();
    expect(screen.getByText('Fail: no new run may be created.')).toBeTruthy();
    expect(screen.getByText('Carryover Mismatch')).toBeTruthy();
  });

  it('has no detectable accessibility violations in the static gallery', async () => {
    await render(AlpacaBotControlExampleComponent);

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });
});
