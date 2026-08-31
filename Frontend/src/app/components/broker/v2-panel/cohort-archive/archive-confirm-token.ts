/**
 * The token an operator types to confirm a batch archive.
 *
 * Mirrors the `required_token` the backend puts on the single-bot archive
 * action's typed confirmation, so one word means one thing across both
 * surfaces. Kept in its own module because the drawer owns the check and the
 * commit control renders the prompt.
 */
export const ARCHIVE_CONFIRM_TOKEN = 'ARCHIVE';
