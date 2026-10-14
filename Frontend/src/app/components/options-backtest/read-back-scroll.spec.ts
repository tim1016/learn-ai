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
  it('does not move for a block that is already inside the frame', () => {
    const container = frame();

    bringIntoView(container, block(150, 250));

    expect(container.scrollTop).toBe(400);
  });

  it('lines up the top of a block above the frame', () => {
    const container = frame();

    bringIntoView(container, block(40, 120));

    expect(container.scrollTop).toBe(340);
  });

  it('lines up the bottom of a block below the frame', () => {
    const container = frame();

    bringIntoView(container, block(280, 340));

    expect(container.scrollTop).toBe(440);
  });

  it('keeps the top of a block taller than the frame in view', () => {
    const container = frame();

    bringIntoView(container, block(250, 650));

    expect(container.scrollTop).toBe(550);
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
