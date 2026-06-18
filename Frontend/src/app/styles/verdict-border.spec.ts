import { compileString } from 'sass';
import { describe, it, expect } from 'vitest';

const PROJECT_ROOT = process.cwd();

function compileWithMixin(verdict: string): string {
  return compileString(
    `
    @use 'src/app/styles/verdict-border' as v;
    .card { @include v.card-verdict-border('${verdict}'); }
    `,
    { loadPaths: [PROJECT_ROOT] },
  ).css;
}

describe('card-verdict-border mixin', () => {
  it('emits border-color referencing --verdict-ready', () => {
    const css = compileWithMixin('ready');

    expect(css).toMatch(/border-color:\s*var\(--verdict-ready\)/);
  });

  it('emits a box-shadow glow referencing --verdict-ready-soft', () => {
    const css = compileWithMixin('ready');

    expect(css).toMatch(/box-shadow:[^;]*var\(--verdict-ready-soft\)/);
  });
});
