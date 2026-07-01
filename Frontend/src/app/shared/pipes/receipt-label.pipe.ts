import { Pipe, PipeTransform } from '@angular/core';

const ACRONYMS = new Map<string, string>([
  ['api', 'API'],
  ['dte', 'DTE'],
  ['ibkr', 'IBKR'],
  ['id', 'ID'],
  ['nyse', 'NYSE'],
  ['pnl', 'P&L'],
  ['rth', 'RTH'],
  ['sse', 'SSE'],
  ['ui', 'UI'],
  ['utc', 'UTC'],
  ['wal', 'WAL'],
]);

const CODE_SEGMENT_PATTERN = /^[A-Za-z0-9_.-]+$/;
const OPAQUE_RECEIPT_VALUE_LABEL_TOKENS = new Set(['id', 'hash', 'path', 'ref', 'url']);

function isCodeLikeSegment(segment: string): boolean {
  const trimmed = segment.trim();
  if (!trimmed) return false;
  return CODE_SEGMENT_PATTERN.test(trimmed);
}

function formatToken(token: string): string {
  const normalized = token.trim();
  if (!normalized) return '';
  const acronym = ACRONYMS.get(normalized.toLowerCase());
  if (acronym) return acronym;
  return normalized.charAt(0).toUpperCase() + normalized.slice(1).toLowerCase();
}

function formatCodeSegment(segment: string): string {
  return segment
    .replace(/[_.-]+/g, ' ')
    .split(/\s+/)
    .map(formatToken)
    .filter(Boolean)
    .join(' ');
}

export function formatReceiptLabel(value: string | null | undefined): string {
  if (value === null || value === undefined) return '';
  const trimmed = value.trim();
  if (!trimmed) return '';

  const segments = trimmed.split(/\s*,\s*/);
  if (segments.every(isCodeLikeSegment)) {
    return segments.map(formatCodeSegment).join(', ');
  }

  return value;
}

export function isOpaqueReceiptValueLabel(label: string | null | undefined): boolean {
  const tokens = label
    ?.trim()
    .toLowerCase()
    .split(/[_.-]+/)
    .filter(Boolean);
  if (!tokens?.length) return false;
  const lastToken = tokens[tokens.length - 1];
  return OPAQUE_RECEIPT_VALUE_LABEL_TOKENS.has(lastToken);
}

export function formatReceiptValue(
  label: string | null | undefined,
  value: string | null | undefined,
): string {
  if (value === null || value === undefined) return '';
  return isOpaqueReceiptValueLabel(label) ? value : formatReceiptLabel(value);
}

@Pipe({
  name: 'receiptLabel',
})
export class ReceiptLabelPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatReceiptLabel(value);
  }
}
