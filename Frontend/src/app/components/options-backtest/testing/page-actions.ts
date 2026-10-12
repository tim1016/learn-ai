import { fireEvent, screen, within } from '@testing-library/angular';

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

const normalised = (text: string | null) => (text ?? '').replace(/\s+/g, ' ').trim();

/** The text of an element as it reads on screen: its own words and those of the components inside it, one space between. */
export function reads(element: Element | null): string {
  return normalised(element?.textContent ?? null);
}

/** The paragraph whose whole text reads like this, wherever it is; a date inside it is a component of its own, so a text query would miss it. */
export function paragraph(text: string | RegExp, within: ParentNode = document): HTMLElement {
  const found = [...within.querySelectorAll<HTMLElement>('p')].find((candidate) => (typeof text === 'string' ? reads(candidate) === text : text.test(reads(candidate))));
  if (found === undefined) throw new Error(`No paragraph reads ${String(text)}.`);
  return found;
}
