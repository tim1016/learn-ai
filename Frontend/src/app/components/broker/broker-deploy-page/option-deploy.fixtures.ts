/**
 * What the server answers about a Deploy of a bot that trades an option
 * structure (#2913), for the specs of Deploy's Money, Confirm and workflow.
 *
 * Pure data, with no test-runner imports. Every object here is an answer the
 * server branch's own test floor gave (a long call on SPY, two structures;
 * a bull call spread; three bear call spreads), cut from its JSON and not
 * retyped: no sentence, label or dollar string is this file's. No spec pins
 * their wording.
 */

import type { components } from '../../../api/broker.types';
import type { BotDeployPrefill } from '../v2-panel/lib/broker-v2-panel.service';

type OptionDeployReview = components['schemas']['OptionDeployReview'];
type UntestedTradedAssetWarning = components['schemas']['UntestedTradedAssetWarning'];

/** The warning for a Dry Run: no sentence to accept. */
export const OPTIONS_WARNING: UntestedTradedAssetWarning = {
  "headline": "Options have no backtest here.",
  "detail": "This strategy's evidence comes from trading SPY shares. Its results trading a long call on SPY on SPY's signal have not been tested. Options also lose value with time and move with volatility, which a share signal does not look at. Nothing here says the trade is profitable.",
  "acceptance_label": null
};

/** The warning for Paper, Live Shadow and Live: with the sentence to tick. */
export const OPTIONS_WARNING_TO_ACCEPT: UntestedTradedAssetWarning = {
  "headline": "Options have no backtest here.",
  "detail": "This strategy's evidence comes from trading SPY shares. Its results trading a long call on SPY on SPY's signal have not been tested. Options also lose value with time and move with volatility, which a share signal does not look at. Nothing here says the trade is profitable.",
  "acceptance_label": "I accept that this strategy is untested trading options on SPY."
};

/** The server's Trades line for the long call. */
export const OPTION_TRADES_SUMMARY = "listens to SPY · trades a long call on SPY · 2 structures";

/** Resolved, before an amount is reviewed: picks and rows, no row for the budget. */
export const OPTION_REVIEW_PRICED: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062400,
  "figures_good_until_ms": 1791297122400,
  "prices_sentence": "Each bought contract is priced at its ask and each sold one at its bid, as quoted when the quotes were read. The bot picks again at every entry, and the prices move.",
  "picks": [
    {
      "number": 1,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$4.12",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    }
  ],
  "money_rows": [
    {
      "key": "set_aside",
      "label": "Set aside for 2 structures",
      "amount_cents": 82409,
      "amount_usd": "$824.09",
      "note": null,
      "part_of_set_aside": false
    },
    {
      "key": "premium",
      "label": "Premium",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "paid",
      "part_of_set_aside": true
    },
    {
      "key": "broker_requirement",
      "label": "Alpaca's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": true
    },
    {
      "key": "fees",
      "label": "Fees (estimate)",
      "amount_cents": 9,
      "amount_usd": "$0.09",
      "note": "Fees are an estimate until Alpaca reports them.",
      "part_of_set_aside": true
    },
    {
      "key": "loss_at_expiry",
      "label": "Loss at expiry, if held, before fees (estimate)",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never exercised or assigned early.",
      "part_of_set_aside": false
    },
    {
      "key": "incremental_requirement",
      "label": "What this adds to the account's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": false,
      "state": "known",
      "as_of_ms": 1791297062400
    },
    {
      "key": "headroom_left",
      "label": "Account headroom left after one entry",
      "amount_cents": 417591,
      "amount_usd": "$4,175.91",
      "note": null,
      "part_of_set_aside": false,
      "state": "known",
      "as_of_ms": 1791297062400
    }
  ],
  "fits": 6,
  "fits_sentence": "6 whole entries fit right now under the account's headroom, the limit with the least room. Every bot competes for the same account headroom, so these counts are never added up across bots.",
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": "Nothing in these picks or their prices would stop an entry right now. An entry is also held to the budget, to the account and to the quotes at its own moment.",
  "blockers": [],
  "notes": []
};

/** Resolved, with a reviewed amount: the rows end with what the budget has free. */
export const OPTION_REVIEW_WITH_BUDGET: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062400,
  "figures_good_until_ms": 1791297122400,
  "prices_sentence": "Each bought contract is priced at its ask and each sold one at its bid, as quoted when the quotes were read. The bot picks again at every entry, and the prices move.",
  "picks": [
    {
      "number": 1,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$4.12",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    }
  ],
  "money_rows": [
    {
      "key": "set_aside",
      "label": "Set aside for 2 structures",
      "amount_cents": 82409,
      "amount_usd": "$824.09",
      "note": null,
      "part_of_set_aside": false
    },
    {
      "key": "premium",
      "label": "Premium",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "paid",
      "part_of_set_aside": true
    },
    {
      "key": "broker_requirement",
      "label": "Alpaca's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": true
    },
    {
      "key": "fees",
      "label": "Fees (estimate)",
      "amount_cents": 9,
      "amount_usd": "$0.09",
      "note": "Fees are an estimate until Alpaca reports them.",
      "part_of_set_aside": true
    },
    {
      "key": "loss_at_expiry",
      "label": "Loss at expiry, if held, before fees (estimate)",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never exercised or assigned early.",
      "part_of_set_aside": false
    },
    {
      "key": "budget_free",
      "label": "Free in this budget after one entry",
      "amount_cents": 17591,
      "amount_usd": "$175.91",
      "note": null,
      "part_of_set_aside": false,
      "state": "known",
      "as_of_ms": 1791297062400
    },
    {
      "key": "incremental_requirement",
      "label": "What this adds to the account's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": false,
      "state": "known",
      "as_of_ms": 1791297062400
    },
    {
      "key": "headroom_left",
      "label": "Account headroom left after one entry",
      "amount_cents": 417591,
      "amount_usd": "$4,175.91",
      "note": null,
      "part_of_set_aside": false,
      "state": "known",
      "as_of_ms": 1791297062400
    }
  ],
  "fits": 1,
  "fits_sentence": "One whole entry fits right now under the bot's budget, the limit with the least room. Every bot competes for the same account headroom, so these counts are never added up across bots.",
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": "Nothing in these picks or their prices would stop an entry right now. An entry is also held to the budget, to the account and to the quotes at its own moment.",
  "blockers": [],
  "notes": []
};

/** No price yet, and a wait before it is worth asking again. */
export const OPTION_REVIEW_AWAITING: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "feed_not_up",
  "preview_reason_code": "OPTION_PREVIEW_FEED_NOT_UP",
  "preview_sentence": "The quote feed is not up: the IBKR market-data connection is not up.",
  "ask_again_in_ms": 1000,
  "quotes_read_ms": null,
  "figures_good_until_ms": null,
  "picks": [],
  "money_rows": [],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": null,
  "blockers": [],
  "notes": []
};

/** The preview was not asked: a structure with no leg. One blocker, with a fix. */
export const OPTION_REVIEW_NOT_ASKED: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Custom",
  "structure_in_a_sentence": "a custom option structure on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "not_asked",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": null,
  "figures_good_until_ms": null,
  "picks": [],
  "money_rows": [],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": null,
  "blockers": [
    {
      "gate_id": "OPTION_PREVIEW_NO_LEGS",
      "label": "Structure",
      "headline": "Add a leg. A structure has one to four.",
      "fix": "Change the structure in the builder, then review the Deploy again."
    }
  ],
  "notes": []
};

/** A spread on Paper: three blockers, the preview still arriving, the standing notes. */
export const OPTION_REVIEW_BLOCKED: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Bull call spread",
  "structure_in_a_sentence": "a bull call spread on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": "This structure has a sold leg, so the account's Clerk closes it 10 minutes before the close of every session it is held through, whether or not the bot is running. Keeping it overnight is not offered.",
  "preview_state": "quotes_arriving",
  "preview_reason_code": "OPTION_PREVIEW_QUOTES_ARRIVING",
  "preview_sentence": "Quotes are arriving. Not every contract these rules can pick has one yet.",
  "ask_again_in_ms": 1000,
  "quotes_read_ms": null,
  "figures_good_until_ms": null,
  "picks": [],
  "money_rows": [],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": null,
  "blockers": [
    {
      "gate_id": "OPTION_STRUCTURE_NOT_SUPPORTED",
      "label": "Structure",
      "headline": "This plan opens 2 option legs. The account Clerk takes one bought call or put for now; structures of several legs are not built yet.",
      "fix": "Deploy a single bought call or put. The builder and the preview still show this structure, and it can be deployed once structures of several legs are built."
    },
    {
      "gate_id": "OPTION_WINDOW_OVER_LINE_BUDGET",
      "label": "IBKR quote lines",
      "headline": "This system counts 1 IBKR quote line free, and this structure can need 10. Its budget of option quote lines is 1, and the option bots running here can need 0. Other IBKR sessions share the same allowance, so the count is its best estimate.",
      "fix": "Raise this Clerk's IBKR_OPTION_LINE_BUDGET, stop another options bot, or deploy a narrower structure."
    },
    {
      "gate_id": "OPTION_DEPLOY_APPROVAL_TOO_LOW",
      "label": "Options approval",
      "headline": "This account is approved for covered calls and cash-secured puts. A structure of several legs needs the spreads level (3).",
      "fix": "Ask Alpaca for spreads approval, or choose a single option."
    }
  ],
  "notes": [
    {
      "code": "OPTION_DEPLOY_NOT_CHECKED_AT_ALPACA",
      "headline": "This system's option orders have not been checked against Alpaca.",
      "detail": "The order, its cancel and its fills are built from Alpaca's published examples, not from what Alpaca has answered. Check this bot's first option orders at Alpaca yourself."
    },
    {
      "code": "OPTION_DEPLOY_FEES_ARE_AN_ESTIMATE",
      "headline": "Option fees on this account are estimates until Alpaca posts them.",
      "detail": "Alpaca reports no fee with a fill. Until its fee rows for a day with an option fill have been seen, each bot's share of them comes from this system's fee model."
    },
    {
      "code": "OPTION_DEPLOY_TRADE_STREAM_NOT_SEEN",
      "headline": "If Alpaca's trade stream does not carry option fills, this account stops taking entries at its first option fill.",
      "detail": "A fill this system learns only by looking the order up is held as unconfirmed, and an account with one takes no new entry for any of its bots until it is confirmed. Whether the stream carries option fills is not known until one Paper order has shown it."
    }
  ]
};

/** A Live Shadow: one blocker, with rows and picks beside it. */
export const OPTION_REVIEW_BLOCKED_WITH_ROWS: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062400,
  "figures_good_until_ms": 1791297122400,
  "prices_sentence": "Each bought contract is priced at its ask and each sold one at its bid, as quoted when the quotes were read. The bot picks again at every entry, and the prices move.",
  "picks": [
    {
      "number": 1,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$4.12",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    }
  ],
  "money_rows": [
    {
      "key": "set_aside",
      "label": "Set aside for 2 structures",
      "amount_cents": 82409,
      "amount_usd": "$824.09",
      "note": null,
      "part_of_set_aside": false
    },
    {
      "key": "premium",
      "label": "Premium",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "paid",
      "part_of_set_aside": true
    },
    {
      "key": "broker_requirement",
      "label": "Alpaca's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": true
    },
    {
      "key": "fees",
      "label": "Fees (estimate)",
      "amount_cents": 9,
      "amount_usd": "$0.09",
      "note": "Fees are an estimate until Alpaca reports them.",
      "part_of_set_aside": true
    },
    {
      "key": "loss_at_expiry",
      "label": "Loss at expiry, if held, before fees (estimate)",
      "amount_cents": 82400,
      "amount_usd": "$824.00",
      "note": "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never exercised or assigned early.",
      "part_of_set_aside": false
    }
  ],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": "Nothing in these picks or their prices would stop an entry right now. An entry is also held to the budget, to the account and to the quotes at its own moment.",
  "blockers": [
    {
      "gate_id": "OPTION_DEPLOY_LIVE_SHADOW_NOT_BUILT",
      "label": "Live Shadow",
      "headline": "A Live Shadow does not take an option bot yet. It is not built.",
      "fix": "Deploy this bot as a Dry Run, or on a Paper account."
    }
  ],
  "notes": [
    {
      "code": "OPTION_DEPLOY_NOT_CHECKED_AT_ALPACA",
      "headline": "This system's option orders have not been checked against Alpaca.",
      "detail": "The order, its cancel and its fills are built from Alpaca's published examples, not from what Alpaca has answered. Check this bot's first option orders at Alpaca yourself."
    },
    {
      "code": "OPTION_DEPLOY_FEES_ARE_AN_ESTIMATE",
      "headline": "Option fees on this account are estimates until Alpaca posts them.",
      "detail": "Alpaca reports no fee with a fill. Until its fee rows for a day with an option fill have been seen, each bot's share of them comes from this system's fee model."
    },
    {
      "code": "OPTION_DEPLOY_TRADE_STREAM_NOT_SEEN",
      "headline": "If Alpaca's trade stream does not carry option fills, this account stops taking entries at its first option fill.",
      "detail": "A fill this system learns only by looking the order up is held as unconfirmed, and an account with one takes no new entry for any of its bots until it is confirmed. Whether the stream carries option fills is not known until one Paper order has shown it."
    }
  ]
};

/** A red verdict that is not about the structure's shape: a blocker with no fix. */
export const OPTION_REVIEW_NO_FIX: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062400,
  "figures_good_until_ms": 1791297122400,
  "prices_sentence": "Each bought contract is priced at its ask and each sold one at its bid, as quoted when the quotes were read. The bot picks again at every entry, and the prices move.",
  "picks": [
    {
      "number": 1,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$4.12",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    }
  ],
  "money_rows": [],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": null,
  "blockers": [
    {
      "gate_id": "OPTION_FEE_RATE_NOT_PINNED",
      "label": "Structure",
      "headline": "Right now this cannot be entered as built: no fee rate is pinned for today, so what an entry sets aside is not known.",
      "fix": null
    }
  ],
  "notes": []
};

/** Three bear call spreads: a premium received, a requirement larger than what is set aside, a sold leg. */
export const OPTION_REVIEW_THAT_PAYS: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Bear call spread",
  "structure_in_a_sentence": "a bear call spread on",
  "structures": 3,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": "This structure has a sold leg, so the account's Clerk closes it 10 minutes before the close of every session it is held through, whether or not the bot is running. Keeping it overnight is not offered.",
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062400,
  "figures_good_until_ms": 1791297122400,
  "prices_sentence": "Each bought contract is priced at its ask and each sold one at its bid, as quoted when the quotes were read. The bot picks again at every entry, and the prices move.",
  "picks": [
    {
      "number": 1,
      "side_label": "Sell",
      "ratio": 1,
      "price_usd": "$5.00",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    },
    {
      "number": 2,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$1.93",
      "contract": {
        "occ_symbol": "SPY261009C00590000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "590"
      }
    }
  ],
  "money_rows": [
    {
      "key": "set_aside",
      "label": "Set aside for 3 structures",
      "amount_cents": 57929,
      "amount_usd": "$579.29",
      "note": null,
      "part_of_set_aside": false
    },
    {
      "key": "premium",
      "label": "Premium",
      "amount_cents": -92100,
      "amount_usd": "$921.00",
      "note": "received",
      "part_of_set_aside": true
    },
    {
      "key": "broker_requirement",
      "label": "Alpaca's requirement",
      "amount_cents": 150000,
      "amount_usd": "$1,500.00",
      "note": null,
      "part_of_set_aside": true
    },
    {
      "key": "fees",
      "label": "Fees (estimate)",
      "amount_cents": 29,
      "amount_usd": "$0.29",
      "note": "Fees are an estimate until Alpaca reports them.",
      "part_of_set_aside": true
    },
    {
      "key": "loss_at_expiry",
      "label": "Loss at expiry, if held, before fees (estimate)",
      "amount_cents": 57900,
      "amount_usd": "$579.00",
      "note": "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never exercised or assigned early.",
      "part_of_set_aside": false
    },
    {
      "key": "budget_free",
      "label": "Free in this budget after one entry",
      "amount_cents": 42071,
      "amount_usd": "$420.71",
      "note": null,
      "part_of_set_aside": false
    }
  ],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": "Nothing in these picks or their prices would stop an entry right now. An entry is also held to the budget, to the account and to the quotes at its own moment.",
  "blockers": [],
  "notes": [
    {
      "code": "OPTION_DEPLOY_LEANS_AGAINST_THE_SIGNAL",
      "headline": "This structure gains when SPY falls.",
      "detail": "Leans down. It moves like about 60 SPY shares sold short."
    },
    {
      "code": "OPTION_DEPLOY_NOT_CHECKED_AT_ALPACA",
      "headline": "This system's option orders have not been checked against Alpaca.",
      "detail": "The order, its cancel and its fills are built from Alpaca's published examples, not from what Alpaca has answered. Check this bot's first option orders at Alpaca yourself."
    },
    {
      "code": "OPTION_DEPLOY_FEES_ARE_AN_ESTIMATE",
      "headline": "Option fees on this account are estimates until Alpaca posts them.",
      "detail": "Alpaca reports no fee with a fill. Until its fee rows for a day with an option fill have been seen, each bot's share of them comes from this system's fee model."
    },
    {
      "code": "OPTION_DEPLOY_TRADE_STREAM_NOT_SEEN",
      "headline": "If Alpaca's trade stream does not carry option fills, this account stops taking entries at its first option fill.",
      "detail": "A fill this system learns only by looking the order up is held as unconfirmed, and an account with one takes no new entry for any of its bots until it is confirmed. Whether the stream carries option fills is not known until one Paper order has shown it."
    }
  ]
};

/** Nothing stops the Deploy, and a note says an entry made now would be skipped. */
export const OPTION_REVIEW_ENTRY_SKIPPED: OptionDeployReview = {
  "underlying": "SPY",
  "structure_label": "Long call",
  "structure_in_a_sentence": "a long call on",
  "structures": 2,
  "orders_summary": "Limit in, market out · gap up to 8% a leg · wait 20 s",
  "one_structure_explanation": "One structure an entry: each leg's contracts are its ratio, and one contract is 100 shares of SPY.",
  "custom_structures_explanation": "A whole number of structures an entry. Each leg's contracts are that number times its ratio.",
  "end_sentence": null,
  "preview_state": "resolved",
  "preview_reason_code": null,
  "preview_sentence": null,
  "ask_again_in_ms": null,
  "quotes_read_ms": 1791297062800,
  "figures_good_until_ms": 1791297122800,
  "picks": [
    {
      "number": 1,
      "side_label": "Buy",
      "ratio": 1,
      "price_usd": "$5.00",
      "contract": {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": 1791576000000,
        "right": "call",
        "strike": "585"
      }
    }
  ],
  "money_rows": [
    {
      "key": "set_aside",
      "label": "Set aside for 2 structures",
      "amount_cents": 100009,
      "amount_usd": "$1,000.09",
      "note": null,
      "part_of_set_aside": false
    },
    {
      "key": "premium",
      "label": "Premium",
      "amount_cents": 100000,
      "amount_usd": "$1,000.00",
      "note": "paid",
      "part_of_set_aside": true
    },
    {
      "key": "broker_requirement",
      "label": "Alpaca's requirement",
      "amount_cents": 0,
      "amount_usd": "$0.00",
      "note": null,
      "part_of_set_aside": true
    },
    {
      "key": "fees",
      "label": "Fees (estimate)",
      "amount_cents": 9,
      "amount_usd": "$0.09",
      "note": "Fees are an estimate until Alpaca reports them.",
      "part_of_set_aside": true
    },
    {
      "key": "loss_at_expiry",
      "label": "Loss at expiry, if held, before fees (estimate)",
      "amount_cents": 100000,
      "amount_usd": "$1,000.00",
      "note": "At expiry, if held, before fees. An estimate: it takes every leg as held to expiry and never exercised or assigned early.",
      "part_of_set_aside": false
    },
    {
      "key": "budget_free",
      "label": "Free in this budget after one entry",
      "amount_cents": 99991,
      "amount_usd": "$999.91",
      "note": null,
      "part_of_set_aside": false
    }
  ],
  "money_sentence": "The budget decides whether an entry may start. It is not a cap on what you can spend or lose. A market exit can do worse than these figures.",
  "entry_sentence": "An entry would be skipped right now, for 2 reasons.",
  "blockers": [],
  "notes": [
    {
      "code": "OPTION_DEPLOY_ENTRY_WOULD_BE_SKIPPED",
      "headline": "An entry would be skipped right now, for 2 reasons.",
      "detail": "Leg 1's bid and ask are 50.00% of its mid apart, over your 8% limit. Opening and closing at once would cost $400.00, which is 40.00% of the $1,000.09 set aside. Your limit is 10%. An entry would be skipped right now. The bot picks again at every entry."
    }
  ]
};

/** Deploy again of a bull call spread: its legs and the four terms it was sealed with. */
export const OPTION_PREFILL: BotDeployPrefill = {
  "source_strategy_instance_id": "spy-ema-20260101-0931",
  "strategy_key": "ema_crossover_signal",
  "symbol": "SPY",
  "traded_symbol": null,
  "trades": {
    "kind": "options",
    "underlying": "SPY",
    "structure": "bull_call_spread",
    "legs": [
      {
        "leg_id": "long_call",
        "position": "long",
        "qty_ratio": 1,
        "right": "call",
        "strike": {
          "selector": "atm"
        },
        "expiry": {
          "selector": "absolute",
          "expiration_ms": 1791576000000
        }
      },
      {
        "leg_id": "short_call",
        "position": "short",
        "qty_ratio": 1,
        "right": "call",
        "strike": {
          "selector": "leg_distance",
          "leg_id": "long_call",
          "dollars": 5.0,
          "tolerance_dollars": 0.0
        },
        "expiry": {
          "selector": "absolute",
          "expiration_ms": 1791576000000
        }
      }
    ],
    "order_terms": {
      "gap_limit_percent": "12.5",
      "entry_wait_seconds": 7,
      "cost_limit_percent": "4",
      "expiry_close_lead_minutes": 90
    }
  },
  "sizing": {
    "preset": "custom",
    "quantity": 3
  },
  "parameters": {},
  "exit_terms": {
    "band_multiple": 2.0,
    "spread_cap_bps": 50.0,
    "exit_allowance_bps": 25.0
  }
};

/** A click refused because the structure has no price. The body of a 409. */
export const NOT_PRICED_REFUSAL = {
  "detail": {
    "outcome": "conflict",
    "receipt_id": null,
    "recorded_at_ms": 1790343060180,
    "message": "Waiting to price this structure. The quote feed is not up: the IBKR market-data connection is not up.",
    "why": "An option bot is deployed only while one entry of its structure can be priced. Nothing was deployed and nothing was sent.",
    "next_action": "Deploy again in a few seconds.",
    "admission": null,
    "reason_code": "OPTION_DEPLOY_NOT_PRICED",
    "submission_settled": true
  }
};

/** A click refused by name for a mode that takes no option bot, before any acceptance is asked. The body of a 409. */
export const MODE_REFUSAL = {
  "detail": {
    "outcome": "conflict",
    "receipt_id": null,
    "recorded_at_ms": 1790343061830,
    "message": "A Live Shadow does not take an option bot yet. It is not built.",
    "why": "Live Shadow stops this Deploy. Nothing was deployed and nothing was sent.",
    "next_action": "Deploy this bot as a Dry Run, or on a Paper account.",
    "admission": null,
    "reason_code": "OPTION_DEPLOY_LIVE_SHADOW_NOT_BUILT",
    "submission_settled": true
  }
};

/** A click refused for the first blocker. The body of a 409. */
export const BLOCKER_REFUSAL = {
  "detail": {
    "outcome": "conflict",
    "receipt_id": null,
    "recorded_at_ms": 1790343060635,
    "message": "This plan opens 2 option legs. The account Clerk takes one bought call or put for now; structures of several legs are not built yet.",
    "why": "Structure stops this Deploy. Nothing was deployed and nothing was sent.",
    "next_action": "Deploy a single bought call or put. The builder and the preview still show this structure, and it can be deployed once structures of several legs are built.",
    "admission": null,
    "reason_code": "OPTION_STRUCTURE_NOT_SUPPORTED",
    "submission_settled": true
  }
};

/** Deploy again of an option bot that was bound with no order terms. The body of a 409. */
export const NO_SEALED_TERMS_REFUSAL = {
  "detail": {
    "outcome": "conflict",
    "receipt_id": null,
    "recorded_at_ms": 1790343060087,
    "message": "spy-ema-20260101-0932 trades option contracts and has no order terms sealed with it.",
    "why": "It was bound before Deploy sealed an option bot's four order terms, so there are none to deploy again with. Nothing was filled in for them.",
    "next_action": "Build the structure again in the Deploy form and set its four order terms there.",
    "admission": null,
    "reason_code": "OPTION_DEPLOY_AGAIN_NO_SEALED_TERMS",
    "submission_settled": false
  }
};
