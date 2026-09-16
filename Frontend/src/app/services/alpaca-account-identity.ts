/** Mirrors AlpacaFleetAdapter.canonical_account_id at the display boundary.
 * Durable custody IDs and command targets retain their original spelling. */
export function sameAlpacaAccount(left: string, right: string | null): boolean {
  return right !== null && left.trim().toLowerCase() === right.trim().toLowerCase();
}

/** Shadow status names custody; the URL always names the external account. */
export function alpacaClerkMatchesAccount(
  status: { account_id: string; authority_kind?: string | null },
  accountId: string,
): boolean {
  const expected = status.authority_kind === 'shadow' ? `shadow:${accountId}` : accountId;
  return sameAlpacaAccount(status.account_id, expected);
}
