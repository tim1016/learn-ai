/**
 * One of two display-only geometry files the money lint does not read (`CHART_GEOMETRY` in `eslint.config.mjs`).
 *
 * Why it exists: a chart's price axis is geometry, and geometry needs numbers. A point is placed at a height, a level is
 * drawn across at a height, a fill's marker sits at a height. Nothing else here may do that, and nothing this file makes
 * is ever shown: `axisPrice` is called by `timeline-plan.ts` only (a contract spec holds that), and what it returns goes
 * to the chart library as a position and nowhere else. Every price a person reads, whether in a legend, a tooltip, a
 * list or a table, is the string the server sent, looked up by the point's instant, never this number formatted back.
 *
 * A number is exact only to about fifteen digits, and the server's text is exact for ever, so the number can differ from
 * the text in a far decimal place. That does not matter for a position on a screen a few hundred pixels tall. It would
 * matter anywhere else, which is why this is confined to chart geometry.
 */

const PLAIN_DECIMAL = /^-?\d+(\.\d+)?$/;

/**
 * The height of a served price on a chart's price axis, or null when the text is not a plain decimal. A text such as
 * `1e5`, `0x10`, ` 7` or `Infinity` would be read as a number by the language and is not what the server writes, so it is
 * refused and the caller says the point cannot be placed.
 */
export function axisPrice(price: string): number | null {
  if (!PLAIN_DECIMAL.test(price)) return null;
  const value = Number(price);
  return Number.isFinite(value) ? value : null;
}
