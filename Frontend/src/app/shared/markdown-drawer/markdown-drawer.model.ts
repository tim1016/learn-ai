/**
 * Typed document registry for the shell-level markdown drawer host.
 *
 * Every document that can appear in the drawer is registered here.
 * Consumers call `MarkdownDrawerService.open(docId, anchor?)` with a
 * `MarkdownDocId` literal; the host looks up the rest.
 *
 * Adding a new document:
 *   1. Add its id to `MarkdownDocId`.
 *   2. Add its descriptor to `MARKDOWN_DOC_REGISTRY`.
 *   3. No changes needed to the host component or the service.
 */

export type MarkdownDocId = 'methodology' | 'golden-search-guide';

export interface MarkdownDocDescriptor {
  /** Absolute app-relative URL of the `.md` asset. */
  readonly src: string;
  /** Short label shown in the drawer header eyebrow. */
  readonly eyebrow: string;
  /** Full title shown in the drawer header. */
  readonly title: string;
  /** Route to the full-page view of this document (opened in a new tab). */
  readonly fullPageRoute: string;
  /** Width of the drawer panel. Defaults to `'min(960px, 92vw)'`. */
  readonly width?: string;
  /** Whether the drawer masks the page. Defaults to `true`; a guide read beside a live chart sets `false`. */
  readonly modal?: boolean;
}

export const MARKDOWN_DOC_REGISTRY: Readonly<Record<MarkdownDocId, MarkdownDocDescriptor>> = {
  methodology: {
    src: '/assets/docs/indicator-reliability-methodology.md',
    eyebrow: 'Reference',
    title: 'Indicator Reliability — Methodology',
    fullPageRoute: '/docs/indicator-reliability-methodology',
    width: 'min(960px, 92vw)',
  },
  'golden-search-guide': {
    src: '/assets/docs/golden-search-guide.md',
    eyebrow: 'Guide',
    title: 'Reading the Golden Search charts',
    fullPageRoute: '/docs/golden-search-guide',
    width: 'min(560px, 92vw)',
    modal: false,
  },
};
