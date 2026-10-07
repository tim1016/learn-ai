import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, onTestFinished, vi } from 'vitest';

import { resourceTarget } from '../../../fleet/resource-target';
import { fakePickerWorld, pickSymbol } from '../../../shared/symbol-picker/testing/fake-picker-world';
import {
  BrokerV2PanelService,
  type OptionStructureCatalogue,
  type OptionStructurePreview,
  type OptionStructurePreviewRequest,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_TYPING_SETTLE_MS } from './deploy-money-step.component';
import { DeployOptionStructureComponent } from './deploy-option-structure.component';
import { startingOrderTerms, type DeployOptionSettings } from './option-structure/option-structure';
import {
  BULL_CALL_SPREAD_LEGS,
  LONG_CALL_LEGS,
  OPTION_CATALOGUE,
  previewThatPays,
  previewWithoutFigures,
  resolvedPreview,
} from './option-structure/option-structure.fixtures';
import { PREVIEW_ASK_AGAIN_LIMIT } from './option-structure/option-structure-preview';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const TERMS = startingOrderTerms(OPTION_CATALOGUE.order_terms);
const SPREAD: DeployOptionSettings = { underlying: 'SPY', structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS, order_terms: TERMS };
const NOTHING_BUILT: DeployOptionSettings = { underlying: 'SPY', structure: null, legs: [], order_terms: TERMS };

function previewService(answer: OptionStructurePreview = resolvedPreview()) {
  return { previewOptionStructure: vi.fn<(target: unknown, body: OptionStructurePreviewRequest) => Promise<OptionStructurePreview>>().mockResolvedValue(answer) };
}

async function renderOptions(
  settings: DeployOptionSettings,
  { service = previewService(), catalogue = OPTION_CATALOGUE, catalogueFailure = null }: {
    service?: ReturnType<typeof previewService>; catalogue?: OptionStructureCatalogue | null; catalogueFailure?: string | null;
  } = {},
) {
  const settingsChange = vi.fn<(settings: DeployOptionSettings) => void>();
  const catalogueRetry = vi.fn();
  const rendered = await render(DeployOptionStructureComponent, {
    inputs: { target: TARGET, settings, catalogue, catalogueFailure, listensTo: 'SPY', structures: 1 },
    on: { settingsChange, catalogueRetry },
    providers: [
      ...fakePickerWorld().providers,
      { provide: DEPLOY_TYPING_SETTLE_MS, useValue: 0 },
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
  return { ...rendered, service, settingsChange, catalogueRetry };
}

/** The builder's own part of the page: the drawer's middle column. */
async function openBuilder(): Promise<HTMLElement> {
  await userEvent.click(screen.getByRole('button', { name: 'Edit structure' }));
  return screen.findByRole('navigation', { name: 'Start from' });
}

describe('DeployOptionStructureComponent', () => {
  it('picks the underlying with the shared symbol picker, starting on the one it was given', async () => {
    const { fixture, settingsChange } = await renderOptions(SPREAD);

    expect(screen.getByRole('combobox', { name: 'Options on' })).toBeTruthy();
    pickSymbol(fixture, 'AAPL');

    expect(settingsChange).toHaveBeenCalledWith({ ...SPREAD, underlying: 'AAPL' });
  });

  it('says a named structure in the server’s line for it, with one Edit button', async () => {
    await renderOptions(SPREAD);

    const summary = screen.getByRole('region', { name: 'Bull call spread' });
    expect(within(summary).getByText('Stand-in: what a bull call spread is.')).toBeTruthy();
    expect(within(summary).getAllByRole('button').map((button) => button.textContent?.trim())).toEqual(['Edit structure']);
    expect(within(summary).queryByRole('list')).toBeNull();
  });

  it('says a structure of the owner’s own leg by leg, in the catalogue’s labels', async () => {
    const [bought] = LONG_CALL_LEGS;
    const own: DeployOptionSettings = {
      ...SPREAD, structure: null, legs: [{ ...bought, strike: { selector: 'delta', target: 0.3, tolerance_delta: 0.05 } }],
    };
    await renderOptions(own);

    const summary = screen.getByRole('region', { name: 'Custom' });
    expect(within(summary).getAllByRole('listitem').map((item) => item.textContent?.trim())).toEqual([
      'Buy 1 call · Delta target 0.3 delta · Nearest weekly',
    ]);
  });

  it('asks for nothing and shows no verdict before the structure has a leg', async () => {
    const { service } = await renderOptions(NOTHING_BUILT);

    expect(within(screen.getByRole('region', { name: 'Custom' })).getByText('Stand-in: what Custom starts from.')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(service.previewOptionStructure).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Right now this picks' })).toBeNull();
    expect(screen.queryByText('Not known yet.')).toBeNull();
  });

  it('shows what the structure picks right now, short, with the verdict above it', async () => {
    const { service } = await renderOptions(SPREAD);

    const picks = screen.getByRole('region', { name: 'Right now this picks' });
    expect(await within(picks).findByRole('button', { name: "Show contract code for SPY Oct 9 '26 585 Call" })).toBeTruthy();
    expect(within(picks).getByRole('button', { name: "Show contract code for SPY Oct 9 '26 590 Call" })).toBeTruthy();
    expect(within(picks).getAllByRole('listitem').map((leg) => leg.querySelector('strong')?.textContent?.trim())).toEqual(['Buy 1', 'Sell 1']);
    // The short view leaves the quotes and deltas to the builder.
    expect(within(picks).queryByText('3.10')).toBeNull();
    expect(picks.querySelector('dl.figures__money')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Premium $1.94 paid · $194.00 a structure');
    for (const sentence of [
      'Stand-in: which way it leans.', 'Stand-in: at expiry, if held, before fees.', 'Stand-in: the most it loses at expiry.',
      'Stand-in: the most it gains at expiry.', 'Stand-in: a market exit can do worse.', 'Stand-in: the bot picks again at every entry.',
    ]) {
      expect(within(picks).getByText(sentence)).toBeTruthy();
    }
    expect(within(picks).getByText(/IBKR quotes read/)).toBeTruthy();
    expect(screen.getByText('Stand-in: it can be deployed.')).toBeTruthy();
    expect(screen.getByText('Stand-in: it resolves to what was built.')).toBeTruthy();
    expect(service.previewOptionStructure.mock.calls[0][1]).toMatchObject({
      underlying: 'SPY', listens_to: 'SPY', structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS, structures: 1,
    });
  });

  it('shows the premium of a structure that pays as received, a share and a structure, never as a minus', async () => {
    await renderOptions(SPREAD, { service: previewService(previewThatPays()) });

    const picks = screen.getByRole('region', { name: 'Right now this picks' });
    await within(picks).findByText('Premium');
    expect(picks.querySelector('dl.figures__money')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Premium $1.20 received · $120.00 a structure');
  });

  it('says a state with nothing to show in the server’s sentence, with no figures', async () => {
    await renderOptions(SPREAD, { service: previewService(previewWithoutFigures('no_quote_line', 'Stand-in: no quote line is spare.')) });

    const picks = screen.getByRole('region', { name: 'Right now this picks' });
    expect(await within(picks).findByText('Stand-in: no quote line is spare.')).toBeTruthy();
    expect(within(picks).queryByRole('list')).toBeNull();
    expect(picks.querySelector('dl')).toBeNull();
  });

  it('stops asking by itself about a state that never passes, and says so beside Refresh', async () => {
    vi.useFakeTimers();
    onTestFinished(() => { vi.useRealTimers(); });
    const service = previewService(previewWithoutFigures('underlying_quote_unusable', 'Stand-in: no fresh quote.', { ask_again_in_ms: 1_000 }));
    await renderOptions(SPREAD, { service });

    await vi.advanceTimersByTimeAsync(5 * 60_000);
    const asked = service.previewOptionStructure.mock.calls.length;
    expect(asked).toBe(1 + PREVIEW_ASK_AGAIN_LIMIT);
    await vi.advanceTimersByTimeAsync(60 * 60_000);

    expect(service.previewOptionStructure).toHaveBeenCalledTimes(asked);
    const picks = screen.getByRole('region', { name: 'Right now this picks' });
    expect(within(picks).getByText('Stand-in: no fresh quote.')).toBeTruthy();
    expect(within(picks).getByText('Stopped asking. Refresh asks again.')).toBeTruthy();
  });

  it('warns in the server’s sentence when the structure leans against the signal', async () => {
    const answer = resolvedPreview();
    const leans = answer.lean && { ...answer.lean, gains_when_underlying_falls: true, warning_sentence: 'Stand-in: this gains when SPY falls.' };
    await renderOptions(SPREAD, { service: previewService({ ...answer, lean: leans }) });

    expect((await screen.findByRole('note')).textContent).toContain('Stand-in: this gains when SPY falls.');
  });

  it('shows no warning for a structure that leans with the signal', async () => {
    await renderOptions(SPREAD);

    await screen.findByText('Stand-in: which way it leans.');
    expect(screen.queryByRole('note')).toBeNull();
  });

  it('says so while what the builder offers is being read, and offers no builder yet', async () => {
    const { service } = await renderOptions(SPREAD, { catalogue: null });

    expect(screen.getByRole('status').textContent).toContain('Reading the option structures…');
    expect(screen.queryByRole('button', { name: 'Edit structure' })).toBeNull();
    expect(service.previewOptionStructure).not.toHaveBeenCalled();
  });

  it('says why what the builder offers could not be read, and offers a retry', async () => {
    const { catalogueRetry } = await renderOptions(SPREAD, { catalogue: null, catalogueFailure: 'Stand-in: the catalogue could not be read.' });

    expect(screen.getByRole('alert').textContent).toContain('Stand-in: the catalogue could not be read.');
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(catalogueRetry).toHaveBeenCalledOnce();
  });

  it('opens the builder over the page on the structure the form holds, and moves the keyboard into it', async () => {
    await renderOptions(SPREAD);
    expect(screen.queryByRole('navigation', { name: 'Start from' })).toBeNull();

    const start = await openBuilder();

    expect(within(start).getByRole('button', { name: 'Bull call spread', pressed: true })).toBeTruthy();
    expect(screen.getByLabelText<HTMLInputElement>('Leg 1 Delta target, delta').value).toBe('0.5');
    expect(start.closest('.p-drawer')).not.toBeNull();
    // It holds the keyboard inside it, so it is a dialog over the page, named by its heading.
    const dialog = screen.getByRole('dialog', { name: 'Structure builder' });
    expect(dialog.contains(start)).toBe(true);
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const builder = start.closest<HTMLElement>('.builder');
    if (builder === null) throw new Error('The builder is not rendered.');
    await vi.waitFor(() => expect(document.activeElement).toBe(within(builder).getByRole('heading', { level: 3, name: 'Bull call spread' })));
  });

  it('takes the built structure into the form, closes, and hands the keyboard back to Edit structure', async () => {
    const { settingsChange } = await renderOptions(SPREAD);
    await openBuilder();
    await userEvent.click(screen.getByRole('button', { name: 'Long call' }));
    // Its leg is on screen, and then the server's verdict on it: only a judged structure is taken.
    await screen.findByRole('heading', { level: 3, name: 'Long call' });
    const useIt = screen.getByRole<HTMLButtonElement>('button', { name: 'Use this structure' });
    await vi.waitFor(() => expect(useIt.disabled).toBe(false));

    await userEvent.click(useIt);

    expect(settingsChange).toHaveBeenCalledWith({ ...SPREAD, structure: 'long_call', legs: LONG_CALL_LEGS });
    await vi.waitFor(() => expect(screen.queryByRole('navigation', { name: 'Start from' })).toBeNull());
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Edit structure' })));
  });

  it.each([
    ['Cancel', () => userEvent.click(screen.getByRole('button', { name: 'Cancel' }))],
    ['the close button', () => userEvent.click(screen.getByRole('button', { name: 'Close the structure builder' }))],
    ['Escape', async () => { fireEvent.keyDown(document, { key: 'Escape', code: 'Escape', which: 27, keyCode: 27 }); }],
  ])('changes nothing on %s, and hands the keyboard back to Edit structure', async (_how, close) => {
    const { settingsChange } = await renderOptions(SPREAD);
    await openBuilder();
    fireEvent.change(screen.getByLabelText('Leg 1 Delta target, delta'), { target: { value: '0.3' } });

    await close();

    await vi.waitFor(() => expect(screen.queryByRole('navigation', { name: 'Start from' })).toBeNull());
    expect(settingsChange).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Edit structure' })));
  });

  it('closes only the calendar on Escape while a leg’s calendar is open, and the builder on the next', async () => {
    const { settingsChange } = await renderOptions(SPREAD);
    await openBuilder();
    fireEvent.change(screen.getByLabelText('Leg 1 Delta target, delta'), { target: { value: '0.3' } });
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'Fixed date');
    const calendar = (): HTMLElement | null => screen.queryByRole('group', { name: 'Calendar' });
    const escapeFrom = (element: Element): void => {
      fireEvent.keyDown(element, { key: 'Escape', code: 'Escape', which: 27, keyCode: 27 });
    };
    const stillBuilding = async (): Promise<void> => {
      await vi.waitFor(() => expect(calendar()).toBeNull());
      const dialog = screen.getByRole('dialog', { name: 'Structure builder' });
      expect(within(dialog).getByRole('navigation', { name: 'Start from' })).toBeTruthy();
      // The page behind stays dimmed, and what was edited is still there.
      expect(document.querySelector('.p-drawer-mask')).not.toBeNull();
      expect(screen.getByLabelText<HTMLInputElement>('Leg 1 Delta target, delta').value).toBe('0.3');
      expect(screen.getByLabelText<HTMLSelectElement>('Leg 1 expiry').selectedOptions[0]?.textContent?.trim()).toBe('Fixed date');
    };
    const open = screen.getByRole('button', { name: 'Open the calendar' });

    // With the keyboard still on the button that opened it.
    await userEvent.click(open);
    expect(calendar()?.closest('.p-drawer')).not.toBeNull();
    escapeFrom(open);
    await stillBuilding();

    // And from a day inside the calendar.
    await userEvent.click(open);
    const day = calendar()?.querySelector('span[data-date]');
    if (!day) throw new Error('The calendar shows no day.');
    escapeFrom(day);
    await stillBuilding();

    // No calendar is open now: Escape is the builder's, from the same field.
    escapeFrom(screen.getByLabelText('Leg 1 expiry date'));
    await vi.waitFor(() => expect(screen.queryByRole('navigation', { name: 'Start from' })).toBeNull());
    expect(settingsChange).not.toHaveBeenCalled();
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Edit structure' })));
  });

  it('stays open, with its edits, when the dimmed page behind it is clicked', async () => {
    await renderOptions(SPREAD);
    await openBuilder();
    fireEvent.change(screen.getByLabelText('Leg 1 Delta target, delta'), { target: { value: '0.3' } });
    const dimmed = document.querySelector('.p-drawer-mask');
    if (dimmed === null) throw new Error('The page behind the builder is not dimmed.');

    await userEvent.click(dimmed);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(screen.getByRole('navigation', { name: 'Start from' })).toBeTruthy();
    expect(screen.getByLabelText<HTMLInputElement>('Leg 1 Delta target, delta').value).toBe('0.3');
  });

  it('has no accessibility violation, with the builder closed and with it open', async () => {
    const answer = resolvedPreview();
    const leans = answer.lean && { ...answer.lean, gains_when_underlying_falls: true, warning_sentence: 'Stand-in: this gains when SPY falls.' };
    await renderOptions(SPREAD, { service: previewService({ ...answer, lean: leans }) });
    await screen.findByRole('note');
    // Landmarks are the page's: on Deploy this sits inside the What step's region.
    const rules = { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } };

    expect((await axe.run(document.body, rules)).violations.map((violation) => violation.id)).toEqual([]);

    await openBuilder();
    await screen.findAllByText('Stand-in: it can be deployed.');
    expect((await axe.run(document.body, rules)).violations.map((violation) => violation.id)).toEqual([]);
  });

  it('asks under one key, the builder while it is open and the summary once it is closed', async () => {
    const { service } = await renderOptions(SPREAD);
    await vi.waitFor(() => expect(service.previewOptionStructure).toHaveBeenCalledTimes(1));
    const key = service.previewOptionStructure.mock.calls[0][1].preview_key;
    expect(key).toMatch(/^[A-Za-z0-9_-]{8,64}$/);

    await openBuilder();
    fireEvent.change(screen.getByLabelText('Leg 1 Delta target, delta'), { target: { value: '0.3' } });
    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.at(-1)?.[1].structure).toBeNull());
    const whileOpen = service.previewOptionStructure.mock.calls.length;

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    // Closed without using it: the summary asks again, about the structure the form still holds.
    await vi.waitFor(() => expect(service.previewOptionStructure.mock.calls.length).toBeGreaterThan(whileOpen));
    expect(service.previewOptionStructure.mock.calls.at(-1)?.[1]).toMatchObject({ structure: 'bull_call_spread', legs: BULL_CALL_SPREAD_LEGS });
    expect(new Set(service.previewOptionStructure.mock.calls.map(([, body]) => body.preview_key))).toEqual(new Set([key]));
  });
});
