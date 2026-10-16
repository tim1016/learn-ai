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
  if (values.length === 0) return [];
  const amounts = scaledDecimals(values);
  const low = amounts.reduce((lowest, amount) => amount < lowest ? amount : lowest);
  const high = amounts.reduce((highest, amount) => amount > highest ? amount : highest);
  return amounts.map((amount, index) => ({
    x: Number(roundedPart(BigInt(index) * 1000n, BigInt(Math.max(1, amounts.length - 1)))),
    y: high === low ? 80 : 12 + Number(roundedPart(136n * (high - amount), high - low)),
  }));
}
