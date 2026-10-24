/**
 * How a workspace declares its tabs, in one place.
 *
 * The account workspace named its tabs once (`ACCOUNT_WORKSPACE_TABS`): the
 * strip renders that list and the window title composes from the same labels,
 * so a tab can never pick up a second name. This is that pattern for any
 * workspace: a workspace calls `defineWorkspaceTabs` once, beside its URL
 * vocabulary, and hands `tabs` to `app-workspace-tab-list`.
 *
 * Presentation only. Where a tab leads, and whether a workspace can offer it
 * at all, stays with the workspace that owns the URL.
 */

/** One tab's identity and its operator-facing name. */
export interface WorkspaceTabDescriptor<Id extends string = string> {
  readonly id: Id;
  readonly label: string;
}

/** A declared tab set: the strip, and the name of every tab the workspace routes. */
export interface WorkspaceTabDeclaration<Id extends string = string> {
  /** The strip's tabs, in the order they are presented. */
  readonly tabs: readonly WorkspaceTabDescriptor<Id>[];
  /** One tab's name. Covers every id in `labels`, including a routed page
   * that is not in the strip, so a window title reads from the same place. */
  label(id: Id): string;
}

/**
 * Declares a workspace's tabs.
 *
 * `labels` names every tab the workspace routes. `strip` lists the ones its
 * tab list shows, in order: a routed page the workspace opens some other way
 * (the account's Deploy, a header button) has a name but no place in the strip.
 *
 * A tab named twice in `strip` is a declaration mistake and throws here, at
 * module load, rather than rendering two links to one place.
 */
export function defineWorkspaceTabs<Id extends string>(
  labels: Readonly<Record<Id, string>>,
  strip: readonly NoInfer<Id>[],
): WorkspaceTabDeclaration<Id> {
  const seen = new Set<Id>();
  for (const id of strip) {
    if (seen.has(id)) throw new Error(`Workspace tab "${id}" is listed twice in the strip.`);
    seen.add(id);
  }
  return {
    tabs: strip.map((id) => ({ id, label: labels[id] })),
    label: (id) => labels[id],
  };
}
