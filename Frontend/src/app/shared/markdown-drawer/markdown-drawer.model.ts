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

export type MarkdownDocId = 'methodology' | 'broker-v2-manual';

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
}

export const MARKDOWN_DOC_REGISTRY: Readonly<Record<MarkdownDocId, MarkdownDocDescriptor>> = {
  methodology: {
    src: '/assets/docs/indicator-reliability-methodology.md',
    eyebrow: 'Reference',
    title: 'Indicator Reliability — Methodology',
    fullPageRoute: '/docs/indicator-reliability-methodology',
    width: 'min(960px, 92vw)',
  },
  'broker-v2-manual': {
    src: '/assets/docs/broker-v2-operator-manual.md',
    eyebrow: 'Operator Manual',
    title: 'Broker V2 Panel',
    fullPageRoute: '/brokers/alpaca/manual',
    width: 'min(800px, 92vw)',
  },
};
