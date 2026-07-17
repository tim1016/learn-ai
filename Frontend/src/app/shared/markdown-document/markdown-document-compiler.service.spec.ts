import { describe, expect, it } from 'vitest';

import type { DocumentListBlock, DocumentTableBlock } from './markdown-document.model';
import { MarkdownDocumentCompiler } from './markdown-document-compiler.service';

describe('MarkdownDocumentCompiler', () => {
  const compiler = new MarkdownDocumentCompiler();

  it('turns canonical Markdown into navigable sections and semantic blocks', () => {
    const document = compiler.compile(`
# Operator Manual

Audience copy.

> A source-grounded note.

## 1. Start safely

Read the **evidence** first.

### 1.1 Verify the offer

1. Run roll call.
2. Start from the fresh offer.

| Gate | Rule |
| --- | --- |
| Roll call | Single-use |

---
`);

    expect(document.title).toBe('Operator Manual');
    expect(document.preamble.some(block => block.kind === 'callout')).toBe(true);
    expect(document.sections).toHaveLength(1);

    const section = document.sections[0];
    expect(section.id).toBe('1-start-safely');
    expect(section.blocks.some(block => block.kind === 'subheading' && block.id === '11-verify-the-offer')).toBe(true);

    const list = section.blocks.find(isDocumentList);
    if (list === undefined) throw new Error('Expected an ordered procedure list.');
    expect(list.ordered).toBe(true);
    expect(list.items).toHaveLength(2);

    const table = section.blocks.find(isDocumentTable);
    if (table === undefined) throw new Error('Expected a decision table.');
    expect(table.headers[0].html).toContain('Gate');
    expect(table.rows[0].cells[1].html).toContain('Single-use');
  });

  it('keeps embedded HTML out of the rendered rich text', () => {
    const document = compiler.compile('## 1. Safe\n\n<script>alert("unsafe")</script>Visible text.');
    const firstBlock = document.sections[0]?.blocks[0];

    expect(firstBlock?.kind).toBe('rich-text');
    if (firstBlock?.kind !== 'rich-text') throw new Error('Expected a rich text block.');
    expect(firstBlock.html).not.toContain('<script>');
    expect(firstBlock.html).toContain('Visible text.');
  });
});

function isDocumentList(block: { readonly kind: string }): block is DocumentListBlock {
  return block.kind === 'list';
}

function isDocumentTable(block: { readonly kind: string }): block is DocumentTableBlock {
  return block.kind === 'table';
}
