import { fireEvent, screen, within } from '@testing-library/angular';

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
