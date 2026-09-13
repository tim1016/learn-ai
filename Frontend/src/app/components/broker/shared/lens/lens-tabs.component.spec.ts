import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { DeskLens } from './lens';
import { LensTabsComponent } from './lens-tabs.component';

async function renderTabs(
  lens: DeskLens = 'trader',
  props: { idPrefix?: string; panelId?: string | null } = {},
) {
  const fixture = await render(LensTabsComponent, {
    componentInputs: {
      lens,
      ...(props.idPrefix !== undefined ? { idPrefix: props.idPrefix } : {}),
      ...(props.panelId !== undefined ? { panelId: props.panelId } : {}),
    },
  });
  return fixture.fixture.componentInstance;
}

describe('LensTabsComponent', () => {
  it('renders an accessible two-tab tablist with roving tabindex', async () => {
    await renderTabs('trader', { idPrefix: 'alpaca' });

    const list = screen.getByRole('tablist', { name: 'Desk perspective' });
    expect(list).toBeTruthy();

    const trader = screen.getByRole('tab', { name: 'Trader' });
    const operator = screen.getByRole('tab', { name: 'Operator' });
    expect(trader.getAttribute('aria-selected')).toBe('true');
    expect(trader.getAttribute('tabindex')).toBe('0');
    expect(operator.getAttribute('aria-selected')).toBe('false');
    expect(operator.getAttribute('tabindex')).toBe('-1');
  });

  it('wires aria-controls and ids to the per-lens panels from the prefix', async () => {
    await renderTabs('trader', { idPrefix: 'alpaca' });

    const trader = screen.getByRole('tab', { name: 'Trader' });
    const operator = screen.getByRole('tab', { name: 'Operator' });
    expect(trader.id).toBe('alpaca-trader-tab');
    expect(trader.getAttribute('aria-controls')).toBe('alpaca-trader-panel');
    expect(operator.id).toBe('alpaca-operator-tab');
    expect(operator.getAttribute('aria-controls')).toBe('alpaca-operator-panel');
  });

  it('drops the leading dash when no prefix is given', async () => {
    await renderTabs('trader');

    const trader = screen.getByRole('tab', { name: 'Trader' });
    expect(trader.id).toBe('trader-tab');
    expect(trader.getAttribute('aria-controls')).toBe('trader-panel');
  });

  it('points both tabs at one shared panel when panelId is set', async () => {
    await renderTabs('trader', { panelId: 'triage-lens-panel' });

    expect(screen.getByRole('tab', { name: 'Trader' }).getAttribute('aria-controls')).toBe('triage-lens-panel');
    expect(screen.getByRole('tab', { name: 'Operator' }).getAttribute('aria-controls')).toBe('triage-lens-panel');
  });

  it('emits the chosen lens on click only when it differs from the current one', async () => {
    const fixture = await render(LensTabsComponent, { componentInputs: { lens: 'trader' } });
    const emitted: DeskLens[] = [];
    fixture.fixture.componentInstance.lensChange.subscribe((lens: DeskLens) => emitted.push(lens));

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    expect(emitted).toEqual(['operator']);

    // The host feeds the adopted lens back in; a repeat click is a no-op.
    fixture.fixture.componentRef.setInput('lens', 'operator');
    fixture.fixture.detectChanges();
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    expect(emitted).toEqual(['operator']);
  });

  it('moves focus with Arrow/Home/End keys and reports the transition', async () => {
    const fixture = await render(LensTabsComponent, { componentInputs: { lens: 'trader' } });
    const emitted: DeskLens[] = [];
    fixture.fixture.componentInstance.lensChange.subscribe((lens: DeskLens) => emitted.push(lens));

    const trader = screen.getByRole('tab', { name: 'Trader' });
    const operator = screen.getByRole('tab', { name: 'Operator' });

    trader.focus();
    fireEvent.keyDown(trader, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(operator);
    expect(emitted).toEqual(['operator']);
    // The component reports the transition; the host owns the selected state,
    // so aria-selected only flips once the host feeds the new lens back in.
    expect(operator.getAttribute('aria-selected')).toBe('false');

    // Host adopted 'operator'; End is now a same-lens no-op.
    fixture.fixture.componentRef.setInput('lens', 'operator');
    fixture.fixture.detectChanges();
    fireEvent.keyDown(operator, { key: 'End' });
    expect(document.activeElement).toBe(operator);
    expect(emitted).toEqual(['operator']);

    fireEvent.keyDown(operator, { key: 'Home' });
    expect(document.activeElement).toBe(trader);
    expect(emitted).toEqual(['operator', 'trader']);

    fireEvent.keyDown(trader, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(trader);
  });

  it('leaves non-transition keys alone', async () => {
    await renderTabs('trader');

    const trader = screen.getByRole('tab', { name: 'Trader' });
    trader.focus();
    fireEvent.keyDown(trader, { key: 'ArrowDown' });

    expect(document.activeElement).toBe(trader);
  });
});
