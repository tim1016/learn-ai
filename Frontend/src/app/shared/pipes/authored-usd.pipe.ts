import { Pipe, PipeTransform } from '@angular/core';

// PRD #2560 D12: Python authors every dollar string and the browser renders it
// as given. Angular's `currency` pipe parses its input before formatting it,
// so it can re-round or reject an authored string; this pipe never parses —
// it formats the authored digits textually, and a string that is not an
// authored amount passes through untouched, so a producer bug shows the raw
// value instead of a transformed figure.

const AUTHORED_AMOUNT = /^(-?)(\d+)(?:\.(\d{1,2}))?$/;
const THOUSANDS = /\B(?=(\d{3})+(?!\d))/g;

export function formatAuthoredUsd(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const authored = AUTHORED_AMOUNT.exec(value);
  if (!authored) return value;
  const [, sign, dollars, cents = ''] = authored;
  return `${sign}$${dollars.replace(THOUSANDS, ',')}.${cents.padEnd(2, '0')}`;
}

@Pipe({
  name: 'authoredUsd',
})
export class AuthoredUsdPipe implements PipeTransform {
  transform(value: string | null | undefined): string | null {
    return formatAuthoredUsd(value);
  }
}
