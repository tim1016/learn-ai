import { describe, expect, it } from 'vitest';

import { bringIntoView, legBlock } from './read-back-scroll';

/** jsdom has no layout, so the spec states the geometry the browser would measure. */
function frame(): HTMLElement {
  const container = document.createElement('div');
  Object.defineProperty(container, 'scrollHeight', { value: 1000, configurable: true });
  Object.defineProperty(container, 'clientHeight', { value: 200, configurable: true });
  container.getBoundingClientRect = () => ({ top: 100, bottom: 300 }) as DOMRect;
  container.scrollTop = 400;
  return container;
}

function block(top: number, bottom: number): HTMLElement {
  const element = document.createElement('div');
  element.getBoundingClientRect = () => ({ top, bottom }) as DOMRect;
  return element;
}

describe('bringing a block of the read-back into view', () => {
  it.each([
    ['does not move for a block that is already inside the frame', 150, 250, 400],
    ['lines up the top of a block above the frame', 40, 120, 340],
    ['lines up the bottom of a block below the frame', 280, 340, 440],
    ['keeps the top of a block taller than the frame in view', 250, 650, 550],
  ])('%s', (_name, top, bottom, scrollTop) => {
    const container = frame();

    bringIntoView(container, block(top, bottom));

    expect(container.scrollTop).toBe(scrollTop);
  });

  it('does not move a frame that holds all of its content', () => {
    const container = frame();
    Object.defineProperty(container, 'scrollHeight', { value: 200, configurable: true });

    bringIntoView(container, block(500, 560));

    expect(container.scrollTop).toBe(400);
  });
});

describe('finding the block of a leg', () => {
  it('finds it by the leg’s id and says nothing for a leg the read-back does not show', () => {
    const readBack = document.createElement('div');
    readBack.innerHTML = '<ul><li data-leg-id="call_2">a</li><li data-leg-id="put">b</li></ul>';

    expect(legBlock(readBack, 'put')?.textContent).toBe('b');
    expect(legBlock(readBack, 'call')).toBeNull();
  });
});
