import { Injectable } from '@angular/core';
import DOMPurify from 'dompurify';
import { Marked, type Token, type Tokens } from 'marked';

import { markdownSlug } from '../markdown/markdown-slug';
import type {
  DocumentBlock,
  DocumentListBlock,
  DocumentSection,
  DocumentTableCell,
  MarkdownDocument,
} from './markdown-document.model';

@Injectable({ providedIn: 'root' })
export class MarkdownDocumentCompiler {
  private readonly marked = new Marked({ gfm: true, breaks: false });

  compile(source: string): MarkdownDocument {
    const preamble: DocumentBlock[] = [];
    const sections: DocumentSection[] = [];
    let title: string | null = null;
    let currentSection: { id: string; ordinal: number; title: string; blocks: DocumentBlock[] } | null = null;
    let blockOrdinal = 0;

    for (const token of this.marked.lexer(source)) {
      if (isSpaceToken(token) || isDefinitionToken(token)) continue;

      if (isHeadingToken(token)) {
        if (token.depth === 1 && title === null) {
          title = this.headingTitle(token);
          continue;
        }

        if (token.depth === 2) {
          currentSection = {
            id: markdownSlug(token.text),
            ordinal: sections.length + 1,
            title: this.headingTitle(token),
            blocks: [],
          };
          sections.push(currentSection);
          continue;
        }

        if (currentSection !== null && token.depth >= 3) {
          currentSection.blocks.push({
            id: markdownSlug(token.text),
            kind: 'subheading',
            level: subheadingLevel(token.depth),
            title: this.headingTitle(token),
          });
        }
        continue;
      }

      const block = this.compileBlock(token, this.nextBlockId(currentSection?.id ?? 'preamble', blockOrdinal++));
      if (block === null) continue;
      (currentSection?.blocks ?? preamble).push(block);
    }

    return { title, preamble, sections };
  }

  private compileBlock(token: Token, id: string): DocumentBlock | null {
    if (isBlockquoteToken(token)) {
      return { id, kind: 'callout', html: this.renderBlocks(token.tokens) };
    }

    if (isListToken(token)) {
      return this.compileList(token, id);
    }

    if (isTableToken(token)) {
      return {
        id,
        kind: 'table',
        headers: token.header.map((cell, index) => this.compileTableCell(cell, `${id}-header-${index}`)),
        rows: token.rows.map((row, rowIndex) => ({
          id: `${id}-row-${rowIndex}`,
          cells: row.map((cell, cellIndex) => this.compileTableCell(cell, `${id}-row-${rowIndex}-cell-${cellIndex}`)),
        })),
      };
    }

    if (isCodeToken(token)) {
      return { id, kind: 'code', language: token.lang ?? null, value: token.text };
    }

    if (isHorizontalRuleToken(token)) {
      return { id, kind: 'divider' };
    }

    return { id, kind: 'rich-text', html: this.renderBlocks([token]) };
  }

  private compileList(token: Tokens.List, id: string): DocumentListBlock {
    return {
      id,
      kind: 'list',
      ordered: token.ordered,
      start: typeof token.start === 'number' ? token.start : null,
      items: token.items.map((item, index) => ({
        id: `${id}-item-${index}`,
        html: this.renderBlocks(item.tokens),
      })),
    };
  }

  private compileTableCell(cell: Tokens.TableCell, id: string): DocumentTableCell {
    return {
      id,
      html: this.sanitize(this.marked.Parser.parseInline(cell.tokens, this.marked.defaults)),
      alignment: cell.align,
    };
  }

  private headingTitle(token: Tokens.Heading): string {
    return token.text.replace(/[`*_~]/g, '');
  }

  private renderBlocks(tokens: Token[]): string {
    return this.sanitize(this.marked.parser(tokens));
  }

  private sanitize(html: string): string {
    return DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ['id', 'class', 'style', 'aria-hidden', 'role'],
    });
  }

  private nextBlockId(sectionId: string, ordinal: number): string {
    return `${sectionId}-block-${ordinal}`;
  }
}

function isBlockquoteToken(token: Token): token is Tokens.Blockquote {
  return token.type === 'blockquote';
}

function isCodeToken(token: Token): token is Tokens.Code {
  return token.type === 'code';
}

function isDefinitionToken(token: Token): token is Tokens.Def {
  return token.type === 'def';
}

function isHeadingToken(token: Token): token is Tokens.Heading {
  return token.type === 'heading';
}

function isHorizontalRuleToken(token: Token): token is Tokens.Hr {
  return token.type === 'hr';
}

function isListToken(token: Token): token is Tokens.List {
  return token.type === 'list';
}

function isSpaceToken(token: Token): token is Tokens.Space {
  return token.type === 'space';
}

function isTableToken(token: Token): token is Tokens.Table {
  return token.type === 'table';
}

function subheadingLevel(depth: number): 3 | 4 | 5 | 6 {
  if (depth === 3 || depth === 4 || depth === 5) return depth;
  return 6;
}
