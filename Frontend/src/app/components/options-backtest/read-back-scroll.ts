/**
 * Scrolls `container` just far enough that `block` is in view, and not at all
 * when it already is. Only the container moves: the page is never scrolled,
 * which `scrollIntoView` would do to reach a block inside a rail that is
 * itself partly off screen.
 */
export function bringIntoView(container: HTMLElement, block: HTMLElement): void {
  if (container.scrollHeight <= container.clientHeight) return;
  const frame = container.getBoundingClientRect();
  const box = block.getBoundingClientRect();
  if (box.top < frame.top) {
    container.scrollTop += box.top - frame.top;
  } else if (box.bottom > frame.bottom) {
    // A block taller than the frame keeps its top in view.
    container.scrollTop += Math.min(box.bottom - frame.bottom, box.top - frame.top);
  }
}

/** The block of the read-back that says the leg with that id, if the read-back shows it. */
export function legBlock(readBack: HTMLElement, legId: string): HTMLElement | null {
  return Array.from(readBack.querySelectorAll<HTMLElement>('[data-leg-id]')).find((block) => block.dataset['legId'] === legId) ?? null;
}
