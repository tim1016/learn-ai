/** #2639 D8–D12: custom Dark Bright Gates saved on the strategy, judged by the
 * data plane, and catalogue indicators the chart computes from the decision
 * candles — through the chart panel, as the bot page and Strategy Lab use it. */
import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { render, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { NEVER, of } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeStrategyChartFactory, type FakeSeries } from '../../../../testing/strategy-chart-fake';
import { fakeStrategyViewDataPlane } from '../../../../testing/strategy-view-data-plane-fakes';
import {
  FAKE_GATE_CATALOGUE,
  FAKE_INDICATOR_CATALOGUE,
  barCloseMs,
  fakeCustomGate,
  fakeStrategyView,
} from '../../../../testing/strategy-view-fixtures';
import type { GateEvaluationResponse } from '../lib/broker-v2-panel.types';
import { BotChartPanelComponent } from './bot-chart-panel.component';
import { STRATEGY_CHART_FACTORY } from './strategy-chart.component';
import { GATE_CANDLE_COLORS } from './strategy-view-model';

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
  CandlestickSeries: 'CandlestickSeries',
  HistogramSeries: 'HistogramSeries',
  LineSeries: 'LineSeries',
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

const GATE_PREFERENCE_KEY = 'broker-v2.strategy-view.gate.v1:foo_cross';
const charts = fakeStrategyChartFactory(vi);
let dataPlane = fakeStrategyViewDataPlane(vi, FAKE_INDICATOR_CATALOGUE, FAKE_GATE_CATALOGUE);

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotChartPanelComponent],
  template: `
    <app-bot-chart-panel symbol="SPY" [view]="view()" [tape]="tape" />
    <ng-template #tape><p>Tape stand-in</p></ng-template>
  `,
})
class PanelHost {
  readonly view = signal(fakeStrategyView());
}

async function renderPanel() {
  const rendered = await render(PanelHost, {
    providers: [{ provide: STRATEGY_CHART_FACTORY, useValue: charts.create }, ...dataPlane.providers],
  });
  await rendered.fixture.whenStable();
  return rendered;
}

function candleColors(): string[] {
  const data: { color: string }[] = charts.current().candles().setData.mock.calls.at(-1)?.[0] ?? [];
  return data.map((candle) => candle.color);
}

function gateDialog(): HTMLElement {
  return screen.getByRole('dialog', { name: 'Dark Bright Gate' });
}

function refused(message: string, status = 422): HttpErrorResponse {
  return new HttpErrorResponse({ status, error: { detail: { code: 'GATE_EXPRESSION_REFUSED', message } } });
}

async function writeDraft(user: ReturnType<typeof userEvent.setup>, label: string, rest: string): Promise<HTMLElement> {
  await user.click(within(gateDialog()).getByRole('button', { name: '+ New gate' }));
  const editor = within(gateDialog()).getByRole('form', { name: 'New gate' });
  // The editor replaced the button that opened it; focus starts at its name.
  await waitFor(() => expect(document.activeElement).toBe(within(editor).getByLabelText('Name')));
  await user.type(within(editor).getByLabelText('Name'), label);
  await user.click(within(editor).getByRole('button', { name: 'FOO7' }));
  await user.type(within(editor).getByLabelText('Expression'), rest);
  return editor;
}

const { upBright, upDark, downBright, downDark } = GATE_CANDLE_COLORS;

describe('BotChartPanelComponent — custom gates and catalogue indicators (#2639)', () => {
  beforeEach(() => {
    charts.created.length = 0;
    dataPlane = fakeStrategyViewDataPlane(vi, FAKE_INDICATOR_CATALOGUE, FAKE_GATE_CATALOGUE);
    localStorage.clear();
  });

  it('lists the strategy’s saved gates after its rule and shades by the data plane’s judgement of them', async () => {
    const user = userEvent.setup();
    const gate = fakeCustomGate();
    dataPlane.gates.list.mockResolvedValue({ strategy_key: 'foo_cross', gates: [gate] });
    dataPlane.gates.evaluate.mockResolvedValue({ results: { [gate.gate_id]: [false, true, true, null] }, chart_computed: [], notices: [] });
    await renderPanel();

    const option = await within(gateDialog()).findByRole('radio', { name: /Foo above close/ });
    expect(option.closest('label')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Foo above close Mine FOO7 - close > 0');
    await user.click(option);

    expect(screen.getByRole('button', { name: /^Gate: Foo above close Mine/ })).toBeTruthy();
    // Up+false, down+true, up+true, down+no result.
    expect(candleColors()).toEqual([upDark, downBright, upBright, downDark]);
    expect(localStorage.getItem(GATE_PREFERENCE_KEY)).toBe(gate.gate_id);
  });

  it('previews a draft written with the variable chips, in the data plane’s judgement, and shows its refusal', async () => {
    const user = userEvent.setup();
    await renderPanel();
    const editor = await writeDraft(user, 'Foo rising', '- close');
    const expression = within(editor).getByLabelText('Expression');
    expect((expression as HTMLInputElement).value).toBe('FOO7 - close');

    dataPlane.gates.evaluate.mockResolvedValue({ results: { draft: [true, true, false, false] }, chart_computed: [], notices: [] });
    await user.click(within(editor).getByRole('button', { name: 'Preview on chart' }));

    expect(dataPlane.gates.evaluate).toHaveBeenLastCalledWith('foo_cross', expect.objectContaining({
      draft: { label: 'Foo rising', expression: 'FOO7 - close', sign: 'gt' },
    }));
    expect(await screen.findByRole('button', { name: /^Gate: Preview · Foo rising/ })).toBeTruthy();
    expect(candleColors()).toEqual([upBright, downBright, upDark, downDark]);
    expect(within(editor).getByRole('status').textContent).toContain('Previewing on the chart');

    dataPlane.gates.evaluate.mockRejectedValue(refused('A gate must be linear: it cannot multiply one variable by another.'));
    await user.clear(expression);
    await user.type(expression, 'FOO7 * close');
    await user.click(within(editor).getByRole('button', { name: 'Preview on chart' }));

    expect((await within(editor).findByRole('alert')).textContent)
      .toContain('A gate must be linear: it cannot multiply one variable by another.');
    // Edited after that preview: its refusal no longer speaks for what is typed.
    await user.type(expression, ' + 1');
    expect(within(editor).queryByRole('alert')).toBeNull();
    expect(within(editor).getByRole('status').textContent).toContain('Changed since the preview');

    await user.click(within(editor).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: /^Gate: Bar 3 in 20–80 Strategy/ })).toBeTruthy();
    expect(within(gateDialog()).queryByRole('form')).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(within(gateDialog()).getByRole('button', { name: '+ New gate' })));
  });

  it('keeps a saved gate’s shading while a newer read is judged, then shows the new answer', async () => {
    const gate = fakeCustomGate();
    localStorage.setItem(GATE_PREFERENCE_KEY, gate.gate_id);
    dataPlane.gates.list.mockResolvedValue({ strategy_key: 'foo_cross', gates: [gate] });
    dataPlane.gates.evaluate.mockResolvedValue({ results: { [gate.gate_id]: [false, true, true, null] }, chart_computed: [], notices: [] });
    const { fixture } = await renderPanel();
    await waitFor(() => expect(candleColors()).toEqual([upDark, downBright, upBright, downDark]));

    let answer: (response: GateEvaluationResponse) => void = () => undefined;
    dataPlane.gates.evaluate.mockReturnValue(new Promise<GateEvaluationResponse>((resolve) => { answer = resolve; }));
    fixture.componentInstance.view.set(fakeStrategyView({ notices: ['A newer read.'] }));
    await screen.findByText('A newer read.');
    expect(candleColors()).toEqual([upDark, downBright, upBright, downDark]);

    answer({ results: { [gate.gate_id]: [true, true, true, true] }, chart_computed: [], notices: [] });
    await waitFor(() => expect(candleColors()).toEqual([upBright, downBright, upBright, downBright]));
  });

  it('never shows one draft’s answer for a newer draft still being judged', async () => {
    const user = userEvent.setup();
    await renderPanel();
    const editor = await writeDraft(user, 'First', '- close');
    dataPlane.gates.evaluate.mockResolvedValue({ results: { draft: [true, true, true, true] }, chart_computed: [], notices: [] });
    await user.click(within(editor).getByRole('button', { name: 'Preview on chart' }));
    expect(await screen.findByRole('button', { name: /^Gate: Preview · First/ })).toBeTruthy();

    let answer: (response: GateEvaluationResponse) => void = () => undefined;
    dataPlane.gates.evaluate.mockReturnValue(new Promise<GateEvaluationResponse>((resolve) => { answer = resolve; }));
    await user.type(within(editor).getByLabelText('Name'), ' again');
    await user.click(within(editor).getByRole('button', { name: 'Preview on chart' }));

    // The first draft's bright candles are not passed off as the second's.
    expect(screen.queryByRole('button', { name: /^Gate: Preview/ })).toBeNull();
    expect(candleColors()).toEqual([upBright, downDark, upDark, downBright]);

    answer({ results: { draft: [false, false, false, false] }, chart_computed: [], notices: [] });
    expect(await screen.findByRole('button', { name: /^Gate: Preview · First again/ })).toBeTruthy();
    expect(candleColors()).toEqual([upDark, downDark, upDark, downDark]);
  });

  it('says so in the editor when the data plane cannot list its catalogue', async () => {
    const user = userEvent.setup();
    dataPlane.gates.catalogue.mockRejectedValue(new HttpErrorResponse({ status: 503 }));
    await renderPanel();
    const editor = await writeDraft(user, 'Any', '- close');

    const catalogue = within(editor).getByRole('region', { name: 'Catalogue' });
    expect(catalogue.textContent).toContain('The data plane did not list its catalogue.');
  });

  it('saves a gate on the strategy and shades by it from then on', async () => {
    const user = userEvent.setup();
    const gate = fakeCustomGate({ label: 'Foo falling', sign: 'lt' });
    dataPlane.gates.create.mockResolvedValue(gate);
    await renderPanel();
    const editor = await writeDraft(user, 'Foo falling', '- close');
    await user.click(within(editor).getByRole('radio', { name: '< 0' }));

    dataPlane.gates.list.mockResolvedValue({ strategy_key: 'foo_cross', gates: [gate] });
    dataPlane.gates.evaluate.mockResolvedValue({ results: { [gate.gate_id]: [true, false, false, true] }, chart_computed: [], notices: [] });
    await user.click(within(editor).getByRole('button', { name: 'Save on strategy' }));

    expect(dataPlane.gates.create).toHaveBeenCalledWith('foo_cross', { label: 'Foo falling', expression: 'FOO7 - close', sign: 'lt' });
    expect(await screen.findByRole('button', { name: /^Gate: Foo falling Mine/ })).toBeTruthy();
    await waitFor(() => expect(candleColors()).toEqual([upBright, downDark, upDark, downBright]));
    expect(localStorage.getItem(GATE_PREFERENCE_KEY)).toBe(gate.gate_id);
    expect(within(gateDialog()).queryByRole('form')).toBeNull();
  });

  it('keeps the editor open with the data plane’s reason when a save is refused', async () => {
    const user = userEvent.setup();
    dataPlane.gates.create.mockRejectedValue(refused('\'FOO8\' is not a value, a setting, a candle field or a catalogue indicator.'));
    await renderPanel();
    const editor = await writeDraft(user, 'Typo', '+ FOO8');

    await user.click(within(editor).getByRole('button', { name: 'Save on strategy' }));

    expect((await within(editor).findByRole('alert')).textContent)
      .toContain('\'FOO8\' is not a value, a setting, a candle field or a catalogue indicator.');
    expect(screen.getByRole('button', { name: /^Gate: Bar 3 in 20–80 Strategy/ })).toBeTruthy();
  });

  it('edits a saved gate in place and deletes one only after a second click', async () => {
    const user = userEvent.setup();
    const gate = fakeCustomGate();
    dataPlane.gates.list.mockResolvedValue({ strategy_key: 'foo_cross', gates: [gate] });
    dataPlane.gates.replace.mockResolvedValue({ ...gate, label: 'Foo well above close' });
    await renderPanel();

    await user.click(await within(gateDialog()).findByRole('button', { name: 'Edit Foo above close' }));
    const editor = within(gateDialog()).getByRole('form', { name: 'Edit gate' });
    const name = within(editor).getByLabelText('Name');
    expect((name as HTMLInputElement).value).toBe('Foo above close');
    await user.clear(name);
    await user.type(name, 'Foo well above close');
    await user.click(within(editor).getByRole('button', { name: 'Save on strategy' }));
    expect(dataPlane.gates.replace).toHaveBeenCalledWith(
      'foo_cross', gate.gate_id, { label: 'Foo well above close', expression: 'FOO7 - close', sign: 'gt' },
    );

    // Closing the editor returns focus to the Edit it came from.
    await waitFor(() => expect(document.activeElement).toBe(within(gateDialog()).getByRole('button', { name: 'Edit Foo above close' })));

    await user.click(within(gateDialog()).getByRole('button', { name: 'Delete Foo above close' }));
    expect(dataPlane.gates.remove).not.toHaveBeenCalled();
    const confirm = within(gateDialog()).getByRole('button', { name: 'Delete “Foo above close”' });
    await waitFor(() => expect(document.activeElement).toBe(confirm));
    await user.click(within(gateDialog()).getByRole('button', { name: 'Keep' }));
    await waitFor(() => expect(document.activeElement).toBe(within(gateDialog()).getByRole('button', { name: 'Delete Foo above close' })));

    await user.click(within(gateDialog()).getByRole('button', { name: 'Delete Foo above close' }));
    dataPlane.gates.list.mockResolvedValue({ strategy_key: 'foo_cross', gates: [] });
    await user.click(within(gateDialog()).getByRole('button', { name: 'Delete “Foo above close”' }));
    expect(dataPlane.gates.remove).toHaveBeenCalledWith('foo_cross', gate.gate_id);
    await waitFor(() => expect(document.activeElement).toBe(within(gateDialog()).getByRole('button', { name: '+ New gate' })));
  });

  it('says so beside the chart when the saved gates cannot be loaded, and still draws the strategy’s rule', async () => {
    dataPlane.gates.list.mockRejectedValue(
      new HttpErrorResponse({ status: 503, error: { detail: { code: 'GATE_STORE_UNAVAILABLE', message: 'The gate store could not be read.' } } }),
    );
    await renderPanel();

    const notices = await screen.findByRole('list', { name: 'Strategy view notices' });
    expect(await within(notices).findByText('Saved gates could not be loaded: The gate store could not be read.')).toBeTruthy();
    expect(candleColors()).toEqual([upBright, downDark, upDark, downBright]);
  });

  it('draws a catalogue indicator the viewer adds, computed on the decision candles, thinner than the bot’s own', async () => {
    const user = userEvent.setup();
    dataPlane.indicators.calculateBars.mockReturnValue(of({
      symbol: 'SPY',
      indicators: [{
        id: 'vwap', color: '#e0c050', panel: 'main', type: 'line',
        data: [{ t: barCloseMs(2), value: 500.5 }, { t: barCloseMs(3), value: 499 }],
      }],
    }));
    await renderPanel();
    const indicators = screen.getByRole('dialog', { name: 'Indicators' });

    await user.type(within(indicators).getByRole('combobox', { name: 'Search indicators' }), 'vwap');
    // jsdom applies the picker's styles in source order, not by specificity, so an
    // open category's rows still compute as display:none here; a browser shows them.
    const row = within(indicators).getAllByRole('option', { hidden: true }).find((option) => option.dataset['name'] === 'vwap');
    if (row === undefined) throw new Error('The catalogue lists no VWAP.');
    await user.click(within(row).getByRole('button', { name: 'Add', hidden: true }));

    const [symbol, bars, entries] = dataPlane.indicators.calculateBars.mock.calls.at(-1) ?? [];
    expect(symbol).toBe('SPY');
    expect(bars[1]).toEqual({ t: barCloseMs(1), o: 502, h: 503, l: 498, c: 499, v: 1_000 });
    expect(entries.map((entry: { name: string }) => entry.name)).toEqual(['vwap']);
    const computedLine = charts.current().series.find(
      (series: FakeSeries) => series.type === 'LineSeries' && series.options['lineWidth'] === 1,
    );
    if (computedLine === undefined) throw new Error('The chart drew no catalogue line.');
    expect(computedLine.pane).toBe(0);
    expect(computedLine.setData.mock.calls.at(-1)?.[0]).toEqual([
      { time: barCloseMs(2) / 1000, value: 500.5 },
      { time: barCloseMs(3) / 1000, value: 499 },
    ]);
    expect(within(screen.getByRole('list', { name: 'Chart legend' })).getByText(/VWAP/).textContent).toContain('chart');

    await user.click(within(indicators).getByRole('button', { name: 'Remove VWAP' }));
    expect(charts.current().chart.removeSeries).toHaveBeenCalledWith(computedLine);
  });

  it('keeps catalogue lines drawn through a re-read, and drops a removed one at once', async () => {
    const user = userEvent.setup();
    const line = (id: string, value: number) => ({
      id, color: '#e0c050', panel: 'main', type: 'line',
      data: [{ t: barCloseMs(2), value }, { t: barCloseMs(3), value: value + 1 }],
    });
    dataPlane.indicators.calculateBars.mockImplementation((_symbol: string, _bars: unknown, entries: { name: string }[]) =>
      of({ symbol: 'SPY', indicators: entries.map((entry) => line(entry.name, entry.name === 'vwap' ? 500 : 9_000)) }));
    const { fixture } = await renderPanel();
    const indicators = screen.getByRole('dialog', { name: 'Indicators' });
    for (const name of ['vwap', 'obv']) {
      const search = within(indicators).getByRole('combobox', { name: 'Search indicators' });
      await user.clear(search);
      await user.type(search, name);
      const row = within(indicators).getAllByRole('option', { hidden: true }).find((option) => option.dataset['name'] === name);
      if (row === undefined) throw new Error(`The catalogue lists no ${name}.`);
      await user.click(within(row).getByRole('button', { name: 'Add', hidden: true }));
    }
    const drawnValues = () => charts.current().series
      .filter((series: FakeSeries) => series.type === 'LineSeries' && series.options['lineWidth'] === 1)
      .slice(-2)
      .map((series: FakeSeries) => series.setData.mock.calls.at(-1)?.[0]?.[0]?.value);
    expect(drawnValues()).toEqual([500, 9_000]);

    // A newer read that is still computing keeps the lines drawn.
    dataPlane.indicators.calculateBars.mockReturnValue(NEVER);
    fixture.componentInstance.view.set(fakeStrategyView({ notices: ['A newer read.'] }));
    await screen.findByText('A newer read.');
    expect(drawnValues()).toEqual([500, 9_000]);

    // Removing one while the newer read computes drops only its line.
    const removedBefore = charts.current().chart.removeSeries.mock.calls.length;
    await user.click(within(indicators).getByRole('button', { name: 'Remove VWAP' }));
    expect(charts.current().chart.removeSeries.mock.calls.length).toBeGreaterThan(removedBefore);
    const remaining = charts.current().series
      .filter((series: FakeSeries) => series.type === 'LineSeries' && series.options['lineWidth'] === 1)
      .at(-1);
    expect(remaining?.setData.mock.calls.at(-1)?.[0]?.[0]?.value).toBe(9_000);
  });

  it('never draws one strategy’s catalogue line on another strategy’s candles', async () => {
    const user = userEvent.setup();
    const addVwap = async () => {
      const indicators = screen.getByRole('dialog', { name: 'Indicators' });
      const search = within(indicators).getByRole('combobox', { name: 'Search indicators' });
      await user.clear(search);
      await user.type(search, 'vwap');
      const row = within(indicators).getAllByRole('option', { hidden: true }).find((option) => option.dataset['name'] === 'vwap');
      if (row === undefined) throw new Error('The catalogue lists no VWAP.');
      await user.click(within(row).getByRole('button', { name: 'Add', hidden: true }));
    };
    const computedLines = () => charts.current().series
      .filter((series: FakeSeries) => series.type === 'LineSeries' && series.options['lineWidth'] === 1);
    dataPlane.indicators.calculateBars.mockReturnValue(of({
      symbol: 'SPY',
      indicators: [{ id: 'vwap', color: '#e0c050', panel: 'main', type: 'line', data: [{ t: barCloseMs(2), value: 500 }] }],
    }));
    const { fixture } = await renderPanel();
    await addVwap();
    expect(computedLines()).toHaveLength(1);

    // Another strategy on the same bars: its own VWAP is still computing.
    dataPlane.indicators.calculateBars.mockReturnValue(NEVER);
    fixture.componentInstance.view.set(fakeStrategyView({ strategy_key: 'bar_cross', strategy_name: 'Bar Cross' }));
    await screen.findByRole('group', { name: /Bar Cross decision candles/ });
    await addVwap();

    expect(computedLines()).toHaveLength(1);
    expect(charts.current().chart.removeSeries).toHaveBeenCalledWith(computedLines()[0]);
  });

  it('passes AXE with the gate editor open', async () => {
    const user = userEvent.setup();
    await renderPanel();
    await writeDraft(user, 'Foo rising', '- close');

    // jsdom lays nothing out, so contrast is checked in the browser, not here.
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
