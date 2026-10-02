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

export function popoverPosition(
  anchor: PopoverAnchor,
  size: { readonly width: number; readonly height: number },
  viewport: { readonly width: number; readonly height: number },
): PopoverPosition {
  const maxLeft = viewport.width - size.width - VIEWPORT_MARGIN_PX;
  const left = Math.max(VIEWPORT_MARGIN_PX, Math.min(anchor.left, maxLeft));
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
