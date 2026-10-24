import { inject } from '@angular/core';
import { Router, type Params, type RedirectFunction, type UrlCreationOptions } from '@angular/router';

/** Where a redirect leads: fixed commands, or commands built from the matched route's own params. */
export type RedirectCommands = readonly string[] | ((params: Params) => readonly string[]);

export interface RedirectStateOptions {
  /** Query keys the destination must not receive: a retired hint, or the key the destination restates. */
  readonly drop?: readonly string[];
  /** Query the destination adds, from the matched route's params. It wins over a key of the same name in the URL. */
  readonly set?: (params: Params) => Params;
}

/** What a redirect reads off the URL it is leaving: a route snapshot has all three. */
export interface RedirectSource {
  readonly params: Params;
  readonly queryParams: Params;
  readonly fragment: string | null;
}

/**
 * The query and fragment a redirect carries to its destination: the old URL's,
 * minus `drop`, plus `set`. For a guard that redirects with its own `UrlTree`
 * and must carry state the same way a `redirectKeepingState` route does.
 */
export function redirectState(
  options: RedirectStateOptions,
  { params, queryParams, fragment }: RedirectSource,
): Pick<UrlCreationOptions, 'queryParams' | 'fragment'> {
  const dropped = new Set(options.drop ?? []);
  const kept = Object.fromEntries(Object.entries(queryParams).filter(([key]) => !dropped.has(key)));
  return { queryParams: { ...kept, ...options.set?.(params) }, fragment: fragment ?? undefined };
}

/**
 * A redirect that lands on its destination in one navigation and carries the
 * old URL's query and fragment with it.
 *
 * An absolute `redirectTo` string cannot: Angular rebuilds the URL from the
 * string alone, so every query key and the fragment it does not restate are
 * lost. A relative one keeps them, but cannot drop a key or take a segment
 * from another route's params. This returns a `UrlTree`, which the router
 * follows within the same navigation, so a bookmark's `?deploy=` intent or
 * `#section` reaches the destination instead of stopping at the redirect.
 */
export function redirectKeepingState(
  commands: RedirectCommands,
  options: RedirectStateOptions = {},
): RedirectFunction {
  return (source) =>
    inject(Router).createUrlTree(
      [...(typeof commands === 'function' ? commands(source.params) : commands)],
      redirectState(options, source),
    );
}
