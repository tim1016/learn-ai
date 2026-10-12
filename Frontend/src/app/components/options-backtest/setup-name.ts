/**
 * The rule for a saved setup's name, mirrored from the server
 * (`saved_setups.py`): 1 to 80 characters of ASCII letters, digits, spaces and
 * - _ . , ( ), starting with a letter or a digit and not ending with a space
 * (so `.` and `..`, which a browser folds out of a URL, are no names). It is here so a name the server
 * would refuse is said at the name; a `/` left to the server would be a 404
 * for a path that does not exist, not a reason. The server still judges.
 */
const SETUP_NAME_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9 _.,()-]{0,78}[A-Za-z0-9_.,()-])?$/;

export const SETUP_NAME_RULE =
  'A name is 1 to 80 characters: letters, digits, spaces and - _ . , ( ). It starts with a letter or a digit and does not end with a space.';

export function isValidSetupName(name: string): boolean {
  return SETUP_NAME_PATTERN.test(name);
}
