import { Pipe, PipeTransform } from '@angular/core';

const ACRONYMS = new Map<string, string>([
  ['api', 'API'],
  ['dte', 'DTE'],
  ['ibkr', 'IBKR'],
  ['id', 'ID'],
  ['nyse', 'NYSE'],
  ['pnl', 'P&L'],
  ['sse', 'SSE'],
  ['ui', 'UI'],
  ['utc', 'UTC'],
  ['wal', 'WAL'],
]);

const CODE_SEGMENT_PATTERN = /^[A-Za-z0-9_.-]+$/;

function isCodeLikeSegment(segment: string): boolean {
  const trimmed = segment.trim();
  if (!trimmed) return false;
  return (
    CODE_SEGMENT_PATTERN.test(trimmed) &&
    (trimmed.includes('_') ||
      trimmed.includes('.') ||
      trimmed.includes('-') ||
      trimmed.toUpperCase() === trimmed)
  );
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

  if (isCodeLikeSegment(trimmed)) {
    return formatCodeSegment(trimmed);
  }

  return value;
}

@Pipe({
  name: 'receiptLabel',
})
export class ReceiptLabelPipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    return formatReceiptLabel(value);
  }
}
