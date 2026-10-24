/** One step of a trail back to a page's group, list or parent. */
export interface BreadcrumbItem {
  readonly label: string;
  /** Router path or commands. Absent: the item is plain text, as the current level is. */
  readonly route?: string | readonly string[];
  /** Carries the list's filters and paging back with the link. */
  readonly queryParams?: Readonly<Record<string, string>>;
}
