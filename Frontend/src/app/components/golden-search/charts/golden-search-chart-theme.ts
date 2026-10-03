import { InjectionToken } from '@angular/core';
import type { TooltipComponentOption } from 'echarts/components';

import { themeColor } from '../../../shared/charts/theme-color';
import type { CandidateKey } from '../golden-search.types';

/** The app's tokens as the colours a chart draws with, read from the element the chart sits in. */
export interface ChartTheme {
  readonly text: string;
  readonly textSecondary: string;
  readonly axis: string;
  readonly gridLine: string;
  readonly tooltipBackground: string;
  readonly warn: string;
  /** A result that loses money. */
  readonly loss: string;
  /** One step below a candidate's value, and one step above it. */
  readonly stepBelow: string;
  readonly stepAbove: string;
  /** A run under a cost stress. */
  readonly stressed: string;
  /** Current settings grey, All-period fit blue, Recent fit amber. */
  readonly candidates: Readonly<Record<CandidateKey, string>>;
}

export function chartTheme(element: HTMLElement): ChartTheme {
  return {
    text: themeColor(element, '--text-primary'),
    textSecondary: themeColor(element, '--text-secondary'),
    axis: themeColor(element, '--border'),
    gridLine: themeColor(element, '--border-light'),
    tooltipBackground: themeColor(element, '--bg-elevated'),
    warn: themeColor(element, '--warn'),
    loss: themeColor(element, '--bear'),
    stepBelow: themeColor(element, '--chart-series-sky'),
    stepAbove: themeColor(element, '--chart-series-blue'),
    stressed: themeColor(element, '--chart-series-teal'),
    candidates: {
      incumbent: themeColor(element, '--text-secondary'),
      all_period: themeColor(element, '--chart-series-blue'),
      recent: themeColor(element, '--chart-series-amber'),
    },
  };
}

/** Swapped for fixed colours in tests, where the app's stylesheet (and so its tokens) is not loaded. */
export const GOLDEN_SEARCH_CHART_THEME = new InjectionToken<(element: HTMLElement) => ChartTheme>('GOLDEN_SEARCH_CHART_THEME', {
  providedIn: 'root',
  factory: () => chartTheme,
});

/** The tooltip box every Golden Search chart shares; each chart supplies its own trigger and formatter. */
export function tooltipFrame(theme: ChartTheme): TooltipComponentOption {
  return {
    confine: true,
    backgroundColor: theme.tooltipBackground,
    borderColor: theme.axis,
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: theme.text, fontSize: 12 },
    extraCssText: 'max-width: 22rem; white-space: normal;',
  };
}

/** How a tooltip, summary or table names a value the study did not record. */
export const NOT_RECORDED = 'not recorded';

/** The hovered index from an axis tooltip's parameters (an array) or an item tooltip's (one object). */
export function dataIndexOf(params: unknown): number | null {
  const first: unknown = Array.isArray(params) ? params[0] : params;
  if (typeof first !== 'object' || first === null || !('dataIndex' in first)) return null;
  const index = first.dataIndex;
  return typeof index === 'number' ? index : null;
}

/** The hovered series' index from an item tooltip's parameters. */
export function seriesIndexOf(params: unknown): number | null {
  if (typeof params !== 'object' || params === null || !('seriesIndex' in params)) return null;
  const index = params.seriesIndex;
  return typeof index === 'number' ? index : null;
}

export interface TooltipRow {
  readonly label: string;
  readonly values: readonly string[];
  /** The mark's colour; a dashed swatch matches a dashed line. */
  readonly swatch?: { readonly color: string; readonly dashed: boolean };
}

/**
 * The hover content rule: what the mark is (`title`), its values with units
 * (`rows`, under `columns`), and the rule, comparison, sample size or window
 * needed to judge them (`notes`). Every string is escaped; labels come from the server.
 */
export interface TooltipContent {
  readonly title: string;
  readonly columns: readonly string[];
  readonly rows: readonly TooltipRow[];
  readonly notes: readonly string[];
}

export function tooltipHtml(content: TooltipContent, theme: ChartTheme): string {
  const cell = 'padding: 2px 0 2px 12px; text-align: right; font-variant-numeric: tabular-nums;';
  const head = content.columns.map((column) => `<th style="${cell} font-weight: 500; color: ${theme.textSecondary};">${escapeHtml(column)}</th>`).join('');
  const rows = content.rows
    .map((row) => {
      const swatch = row.swatch === undefined ? '' : `<i style="display: inline-block; width: 12px; margin-right: 6px; vertical-align: middle; border-top: 2px ${row.swatch.dashed ? 'dashed' : 'solid'} ${row.swatch.color};"></i>`;
      const values = row.values.map((value) => `<td style="${cell}">${escapeHtml(value)}</td>`).join('');
      return `<tr><th style="padding: 2px 0; text-align: left; font-weight: 400;">${swatch}${escapeHtml(row.label)}</th>${values}</tr>`;
    })
    .join('');
  const notes = content.notes.map((note) => `<p style="margin: 6px 0 0; color: ${theme.textSecondary};">${escapeHtml(note)}</p>`).join('');
  return `<p style="margin: 0 0 4px; font-weight: 600;">${escapeHtml(content.title)}</p><table style="border-collapse: collapse;"><thead><tr><th></th>${head}</tr></thead><tbody>${rows}</tbody></table>${notes}`;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
