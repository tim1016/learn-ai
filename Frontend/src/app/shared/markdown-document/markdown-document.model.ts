export interface MarkdownDocument {
  readonly title: string | null;
  readonly preamble: readonly DocumentBlock[];
  readonly sections: readonly DocumentSection[];
}

export interface DocumentSection {
  readonly id: string;
  readonly ordinal: number;
  readonly title: string;
  readonly blocks: readonly DocumentBlock[];
}

interface DocumentBlockBase {
  readonly id: string;
}

export interface DocumentRichTextBlock extends DocumentBlockBase {
  readonly kind: 'rich-text';
  readonly html: string;
}

export interface DocumentCalloutBlock extends DocumentBlockBase {
  readonly kind: 'callout';
  readonly html: string;
}

export interface DocumentSubheadingBlock extends DocumentBlockBase {
  readonly kind: 'subheading';
  readonly level: 3 | 4 | 5 | 6;
  readonly title: string;
}

export interface DocumentListItem {
  readonly id: string;
  readonly html: string;
}

export interface DocumentListBlock extends DocumentBlockBase {
  readonly kind: 'list';
  readonly ordered: boolean;
  readonly start: number | null;
  readonly items: readonly DocumentListItem[];
}

export interface DocumentTableCell {
  readonly id: string;
  readonly html: string;
  readonly alignment: 'center' | 'left' | 'right' | null;
}

export interface DocumentTableRow {
  readonly id: string;
  readonly cells: readonly DocumentTableCell[];
}

export interface DocumentTableBlock extends DocumentBlockBase {
  readonly kind: 'table';
  readonly headers: readonly DocumentTableCell[];
  readonly rows: readonly DocumentTableRow[];
}

export interface DocumentCodeBlock extends DocumentBlockBase {
  readonly kind: 'code';
  readonly language: string | null;
  readonly value: string;
}

export interface DocumentDividerBlock extends DocumentBlockBase {
  readonly kind: 'divider';
}

export type DocumentBlock =
  | DocumentRichTextBlock
  | DocumentCalloutBlock
  | DocumentSubheadingBlock
  | DocumentListBlock
  | DocumentTableBlock
  | DocumentCodeBlock
  | DocumentDividerBlock;
