// PRD #607 / Slice 8 (#615) — centralized IBKR portal URLs.
//
// The IBKR Account Management URL (and any future IBKR portal entry
// points) lives here as the SINGLE source of truth.  The cockpit
// (fleet-header paper-reset, etc.) consumes the constant from this
// module; no other cockpit file hard-codes the URL inline.
//
// A small regression test asserts the URL is a well-formed https URL
// pointing at the IBKR domain so a future edit cannot silently send
// operators to a typo'd or attacker-controlled host.

export const IBKR_PORTAL = {
  ACCOUNT_MANAGEMENT_URL: 'https://www.interactivebrokers.com/portal/',
} as const;
