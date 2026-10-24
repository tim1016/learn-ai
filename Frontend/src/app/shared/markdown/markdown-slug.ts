/** Produces the stable, GitHub-style fragment IDs used across rendered Markdown. */
export function markdownSlug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/^-+|-+$/g, '');
}
