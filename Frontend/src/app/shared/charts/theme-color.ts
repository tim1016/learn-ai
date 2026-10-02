/** A theme token's resolved colour on `element`; a missing token throws rather than drawing in a default colour. */
export function themeColor(element: HTMLElement, token: string): string {
  const color = getComputedStyle(element).getPropertyValue(token).trim();
  if (color.length === 0) throw new Error(`Required theme token ${token} is not defined`);
  return color;
}
