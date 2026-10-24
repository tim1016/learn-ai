/**
 * The Options bot handoff to Deploy (PRD #3490 R23, I02): `?options_setup=<id>`
 * carries one approved options version, by its own Golden Options id, from an
 * options setup's header through the account list to the chosen account's
 * Deploy, which reads it again at its own boundary.
 *
 * It is the key options Deploy has always read. There is no other key for it,
 * and a registered signal program that trades options never uses it: that
 * program deploys through `strategy` and `golden_qualification`, because the
 * split is the strategy model and not the asset traded.
 */
export const OPTIONS_SETUP_QUERY_PARAM = 'options_setup';

/**
 * A Golden Options id as the server accepts one (`ogs_` and 32 hex digits, or 40 more
 * when a golden search made it). A value that does not match names no version, so no
 * read is made for it and nothing is shown in its place.
 */
const OPTIONS_SETUP_ID_PATTERN = /^ogs_[a-f0-9]{32}(?:[a-f0-9]{8})?$/;

export function isOptionsSetupId(value: string | null): value is string {
  return value !== null && OPTIONS_SETUP_ID_PATTERN.test(value);
}

/** What the account list carries for one approved options version: its own id, and nothing else. */
export function optionsDeployHandoffQuery(optionsSetupId: string): Record<string, string> {
  return { [OPTIONS_SETUP_QUERY_PARAM]: optionsSetupId };
}
