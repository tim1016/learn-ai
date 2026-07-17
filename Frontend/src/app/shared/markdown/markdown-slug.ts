/** Produces the stable, GitHub-style fragment IDs used across rendered Markdown. */
export function markdownSlug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/** Produces a CSS-safe anchor for headings rendered by the structured document compiler. */
export function documentAnchor(text: string): string {
  return `document-${markdownSlug(text)}`;
}
