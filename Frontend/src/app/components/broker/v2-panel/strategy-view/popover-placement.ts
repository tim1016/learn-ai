/** Where a popover opens: below its anchor, flipped above when it would run
 * off the bottom, and kept inside the viewport either way. */

export interface PopoverAnchor {
  readonly left: number;
  readonly top: number;
  readonly bottom: number;
}

export interface PopoverPosition {
  readonly left: number;
  readonly top: number;
}

const VIEWPORT_MARGIN_PX = 8;

/** The left edge nearest `left` that keeps a box `width` wide inside the viewport. */
export function leftInsideViewport(left: number, width: number, viewportWidth: number): number {
  const maxLeft = viewportWidth - width - VIEWPORT_MARGIN_PX;
  return Math.max(VIEWPORT_MARGIN_PX, Math.min(left, maxLeft));
}

export function popoverPosition(
  anchor: PopoverAnchor,
  size: { readonly width: number; readonly height: number },
  viewport: { readonly width: number; readonly height: number },
): PopoverPosition {
  const left = leftInsideViewport(anchor.left, size.width, viewport.width);
  const below = anchor.bottom + VIEWPORT_MARGIN_PX;
  const fitsBelow = below + size.height <= viewport.height - VIEWPORT_MARGIN_PX;
  const top = fitsBelow ? below : anchor.top - size.height - VIEWPORT_MARGIN_PX;
  return { left, top: Math.max(VIEWPORT_MARGIN_PX, top) };
}

/** Open a native `[popover]` element (where the browser supports it) at the anchor. */
export function openPopoverAt(element: HTMLElement, anchor: PopoverAnchor): void {
  if (typeof element.showPopover === 'function' && !element.matches(':popover-open')) {
    element.showPopover();
  }
  placePopover(element, anchor);
}

export function placePopover(element: HTMLElement, anchor: PopoverAnchor): void {
  const { left, top } = popoverPosition(
    anchor,
    { width: element.offsetWidth, height: element.offsetHeight },
    { width: window.innerWidth, height: window.innerHeight },
  );
  element.style.left = `${left}px`;
  element.style.top = `${top}px`;
}

/** Slides a dropdown laid out under its anchor sideways, as little as it
 * can, so none of it runs off either side of the window -- a narrow phone
 * otherwise cuts it off. Its CSS caps its width at the window less both
 * margins, so a slide is always enough. */
export function keepInsideViewport(element: HTMLElement): void {
  element.style.translate = '';
  const { left, width } = element.getBoundingClientRect();
  const shift = leftInsideViewport(left, width, window.innerWidth) - left;
  element.style.translate = shift === 0 ? '' : `${shift}px 0`;
}

/** Close a native `[popover]` element where the browser supports it. */
export function closePopover(element: HTMLElement): void {
  if (typeof element.hidePopover === 'function' && element.matches(':popover-open')) {
    element.hidePopover();
  }
}

/** The anchor below a trigger element. */
export function anchorBelow(element: HTMLElement): PopoverAnchor {
  const rect = element.getBoundingClientRect();
  return { left: rect.left, top: rect.top, bottom: rect.bottom };
}
