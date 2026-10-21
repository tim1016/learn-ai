/** ADR 0068 permits mapping exact authored values to chart geometry. Nothing here returns a displayed financial figure. */
export function isLoss(value: string): boolean {
  return value.startsWith('-') && !/^-0+(?:\.0+)?$/.test(value);
}

// The wire keeps exact decimal dollars. Convert them to a common integer scale
// before projecting to pixels so a small change on a large account stays visible.
function scaledDecimals(values: readonly string[]): bigint[] {
  const parts = values.map((value) => value.split('.'));
  const places = Math.max(0, ...parts.map(([, fraction = '']) => fraction.length));
  return parts.map(([whole, fraction = '']) => BigInt(`${whole}${fraction.padEnd(places, '0')}`));
}

function magnitude(value: bigint): bigint {
  return value < 0n ? -value : value;
}

function roundedPart(numerator: bigint, denominator: bigint): bigint {
  const whole = numerator / denominator;
  const remainder = numerator % denominator;
  const twice = remainder * 2n;
  return twice > denominator || (twice === denominator && whole % 2n === 1n) ? whole + 1n : whole;
}

function oneDecimal(value: bigint): string {
  return `${value / 10n}.${value % 10n}`;
}

export function scaledBarPercents(values: readonly string[], maximumPercent: number): string[] {
  const amounts = scaledDecimals(values).map(magnitude);
  const highest = amounts.reduce((largest, amount) => amount > largest ? amount : largest, 0n);
  return amounts.map((amount) => {
    if (amount === 0n) return '0.0';
    const tenths = roundedPart(amount * BigInt(maximumPercent) * 10n, highest);
    return oneDecimal(tenths < 10n ? 10n : tenths);
  });
}

export function heatStrengths(values: readonly string[]): string[] {
  const amounts = scaledDecimals(values).map(magnitude);
  const highest = amounts.reduce((largest, amount) => amount > largest ? amount : largest, 0n);
  return amounts.map((amount) => {
    if (amount === 0n) return '0';
    const thousandths = 160n + roundedPart(680n * amount, highest);
    return `${thousandths / 1000n}.${(thousandths % 1000n).toString().padStart(3, '0')}`;
  });
}

export function filledSharePercent(filled: number, refused: number): string {
  const total = filled + refused;
  return total === 0 ? '0.0' : oneDecimal(roundedPart(BigInt(filled) * 1000n, BigInt(total)));
}

export function equityCoordinates(values: readonly string[]): { x: number; y: number }[] {
  return equityPlot(values)?.coordinates ?? [];
}

/** Common session positions for every plot, with room for the endpoint bars. */
export function sessionX(slot: number, sessions: number): number {
  return 8 + (slot / Math.max(1, sessions)) * 984;
}

export function sessionPlotWidth(sessions: number): number {
  return Math.max(900, sessions * 8);
}

/** Symmetric daily bars; these quantities are pixels, never financial values. */
export function dailyBarGeometry(values: readonly string[]) {
  const heights = scaledBarPercents(values, 50);
  const width = Math.min(14, 700 / Math.max(1, values.length));
  return values.map((value, index) => {
    const height = Number(heights[index]) * 1.6;
    const x = sessionX(index + 1, values.length);
    return { x, left: x - width / 2, width, y: isLoss(value) ? 80 : 80 - height, height: height || 2 };
  });
}

/** Exact source magnitude for a symmetric axis; only its pixels use numeric projection. */
export function barMagnitude(values: readonly string[]): string {
  if (values.length === 0) return '0';
  const amounts = scaledDecimals(values).map(magnitude);
  const highest = amounts.reduce((largest, amount) => amount > largest ? amount : largest, 0n);
  const original = values[amounts.indexOf(highest)];
  if (highest === 0n) return '0';
  return original.startsWith('-') ? original.slice(1) : original;
}

/** Axis labels come from exact bounds, never from rounded pixel coordinates (ADR 0068). */
export function equityPlot(values: readonly string[]) {
  if (values.length === 0) return null;
  const amounts = scaledDecimals(values);
  const low = amounts.reduce((lowest, amount) => amount < lowest ? amount : lowest);
  const high = amounts.reduce((highest, amount) => amount > highest ? amount : highest);
  return {
    low: values[amounts.indexOf(low)],
    high: values[amounts.indexOf(high)],
    coordinates: amounts.map((amount, index) => ({
      x: Number(roundedPart(BigInt(index) * 1000n, BigInt(Math.max(1, amounts.length - 1)))),
      y: high === low ? 80 : 12 + Number(roundedPart(136n * (high - amount), high - low)),
    })),
  };
}

/** ADR 0068: nulls break display paths; only measured authored values set the pixel scale. */
export function segmentedEquityPlot(values: readonly (string | null)[]) {
  const measured = values.flatMap((value, index) => value === null ? [] : [{ value, index }]);
  const scale = equityPlot(measured.map(point => point.value));
  if (scale === null) return null;
  const geometry = new Map(measured.map((point, index) => [point.index, {
    x: point.index * 1000 / Math.max(1, values.length - 1), y: scale.coordinates[index].y,
  }]));
  const segments: string[] = [];
  let segment: string[] = [];
  values.forEach((value, index) => {
    const pixel = geometry.get(index);
    if (value === null || !pixel) {
      if (segment.length) segments.push(segment.join(' '));
      segment = [];
    } else segment.push(`${pixel.x},${pixel.y}`);
  });
  if (segment.length) segments.push(segment.join(' '));
  return { segments, geometry, high: scale.high, low: scale.low };
}
