import { TestBed } from '@angular/core/testing';
import { fireEvent, screen, within } from '@testing-library/angular';
import { expect } from 'vitest';

import { EXAMPLE_ONE_REENTRY } from './fixtures';

/** What an owner does on the page, one helper each, so a spec reads as the story it tells. */

export function typeInto(label: string, value: string): void {
  fireEvent.input(screen.getByLabelText(label), { target: { value } });
}

export function choose(name: string): void {
  fireEvent.click(screen.getByRole('radio', { name }));
}

export function press(name: string): void {
  fireEvent.click(screen.getByRole('button', { name }));
}

export function flip(name: string): void {
  fireEvent.click(screen.getByRole('switch', { name }));
}

/** Add a leg for `right`: pick the pill, then press Add leg. */
export function addLeg(right: 'Call' | 'Put' = 'Call'): void {
  choose(right);
  press('Add leg');
}

export function legCard(position: number): HTMLElement {
  return screen.getByRole('article', { name: `Leg ${position}` });
}

export function inLeg(position: number) {
  return within(legCard(position));
}

export function isDisabled(element: HTMLElement): boolean {
  return (element as HTMLInputElement | HTMLButtonElement).disabled;
}

/** Type a start and an end date into the shared date picker, as an owner does: each applies when its field is left. */
export function pickDates(from: string, to: string): void {
  fireEvent.change(screen.getByLabelText('Start date'), { target: { value: from } });
  fireEvent.change(screen.getByLabelText('End date'), { target: { value: to } });
}

export function dateFields(): { from: string; to: string } {
  return {
    from: (screen.getByLabelText('Start date') as HTMLInputElement).value,
    to: (screen.getByLabelText('End date') as HTMLInputElement).value,
  };
}

/** Open a shipped example from the side list, which gives a complete setup with two legs. */
export async function openExample(name = EXAMPLE_ONE_REENTRY): Promise<void> {
  const list = screen.getByRole('complementary', { name: 'Saved setups' });
  fireEvent.click(await within(list).findByRole('button', { name: new RegExp(`^${name}`) }));
  await screen.findByRole('article', { name: 'Leg 2' });
}

export const checkButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Check data' });

/**
 * The text of an element as it reads on screen: every piece of text inside it, trimmed, one space between pieces.
 * A figure and its label are in separate elements, so `textContent` alone would run them together.
 */
export function reads(element: Element | null): string {
  if (element === null) return '';
  const pieces: string[] = [];
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const text = (node.textContent ?? '').replace(/\s+/g, ' ').trim();
    if (text !== '') pieces.push(text);
  }
  return pieces.join(' ');
}

/** The paragraph whose whole text reads like this, wherever it is; a date inside it is a component of its own, so a text query would miss it. */
export function paragraph(text: string | RegExp, within: ParentNode = document): HTMLElement {
  const found = [...within.querySelectorAll<HTMLElement>('p')].find((candidate) => (typeof text === 'string' ? reads(candidate) === text : text.test(reads(candidate))));
  if (found === undefined) throw new Error(`No paragraph reads ${String(text)}.`);
  return found;
}

/**
 * Lets pending promises finish and the page draw what they left. A spec that asserts something did NOT happen needs
 * it: the page draws a moment after a signal changes, so an assertion straight after would pass before the page showed anything.
 */
export async function lettingGo(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  TestBed.tick();
}

/**
 * Every bare date inside the element is a trading date, so it must be drawn in New York dates and never in the viewer's:
 * a session's open is the afternoon in Europe and the middle of the night in Asia, and the date there is not the session's.
 */
export function expectTradingDatesInNewYork(container: ParentNode): void {
  const dates = [...container.querySelectorAll('app-timestamp-display')].filter((stamp) => /^\d{4}-\d{2}-\d{2}$/.test(reads(stamp)));
  expect(dates.length).toBeGreaterThan(0);
  for (const date of dates) expect(date.getAttribute('data-timestamp-mode')).toBe('date-et');
}
