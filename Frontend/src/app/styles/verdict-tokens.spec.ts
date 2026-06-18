import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';

const VERDICT_TOKENS = [
  '--verdict-ready',
  '--verdict-ready-soft',
  '--verdict-degraded',
  '--verdict-degraded-soft',
  '--verdict-blocked',
  '--verdict-blocked-soft',
  '--verdict-unknown',
  '--verdict-unknown-soft',
  '--verdict-paper',
  '--verdict-paper-soft',
  '--verdict-unsafe',
  '--verdict-unsafe-soft',
] as const;

const TOKENS_SCSS = readFileSync(
  join(process.cwd(), 'src/app/styles/_tokens.scss'),
  'utf8',
);

describe('verdict tokens', () => {
  it.each(VERDICT_TOKENS)('%s is declared in _tokens.scss', (token) => {
    const declaration = new RegExp(`${token}\\s*:`);

    expect(TOKENS_SCSS).toMatch(declaration);
  });
});
