// Generated from the attributed retained read fixtures in PythonDataService/tests/fixtures/options_evidence.
import type { EvidenceWorkspace } from './options-evidence.service';

export const RUN_WORKSPACE = {
  "subject_kind": "run",
  "subject_id": "76",
  "evidence_identity": "run:76:d1b43858a5665309c247592c4c93bbc51cbd3db2b75a8fbd5632d9aab1108b8a",
  "name": "SPY CALL35 PUT50 both35stop no reentry 0945-1545 combined control 2026-10-18",
  "setup": {
    "version": 1,
    "underlying": "SPY",
    "entry": {
      "anchor": "open",
      "offset_ms": 900000
    },
    "exit": {
      "anchor": "close",
      "offset_ms": 900000
    },
    "no_reentry_after": null,
    "square_off": "partial",
    "legs": [
      {
        "leg_id": "call_2",
        "right": "call",
        "contracts": 1,
        "expiry": {
          "kind": "same_day"
        },
        "strike": {
          "kind": "atm"
        },
        "stop_loss": {
          "kind": "percent",
          "value": "35"
        },
        "momentum": {
          "kind": "percent_up",
          "value": "35"
        },
        "reentry_on_stop": null
      },
      {
        "leg_id": "put_atm",
        "right": "put",
        "contracts": 1,
        "expiry": {
          "kind": "same_day"
        },
        "strike": {
          "kind": "atm"
        },
        "stop_loss": {
          "kind": "percent",
          "value": "35"
        },
        "momentum": {
          "kind": "percent_up",
          "value": "50"
        },
        "reentry_on_stop": null
      }
    ],
    "slippage_percent": "0.3",
    "price_step_usd": "0.01",
    "fees": {
      "kind": "per_contract",
      "per_contract_usd": "0.65"
    },
    "starting_cash_usd": "10000"
  },
  "setup_sha256": "d1b43858a5665309c247592c4c93bbc51cbd3db2b75a8fbd5632d9aab1108b8a",
  "source_run_id": 76,
  "study_id": null,
  "period": {
    "first_session_open_ms": 1729517400000,
    "last_session_open_ms": 1792157400000,
    "scheduled_sessions": 499,
    "observed_sessions": 499
  },
  "headline": "Profitable in replay. Research qualification is missing.",
  "replay": {
    "status": "profitable",
    "explanation": "17109.10 USD after recorded modeled costs across 499 observed sessions. Descriptive replay evidence."
  },
  "coverage": {
    "status": "complete",
    "explanation": "499 observed / 499 scheduled sessions. Session coverage does not certify minute valuation coverage."
  },
  "research": {
    "status": "not_recorded",
    "explanation": "No linked search qualification."
  },
  "final": {
    "status": "not_recorded",
    "explanation": "Final research evidence is not recorded for this replay."
  },
  "approval": {
    "status": "recorded",
    "explanation": "Approved from backtest."
  },
  "approvals": [
    {
      "golden_id": "ogs_dbe79f85399048f4a4decc3c86e1f3ff",
      "source_run_id": 76,
      "setup_sha256": "d1b43858a5665309c247592c4c93bbc51cbd3db2b75a8fbd5632d9aab1108b8a",
      "source": "backtest",
      "identity_matches": true,
      "proof_status": "ready",
      "proof_explanation": "This exact tested setup is owner-approved and its repeatability proof matches the running rules.",
      "note": "This is approved for option bots",
      "study_id": null,
      "study_manifest_sha256": null,
      "research_verdict": null,
      "failed_rules": [],
      "waived_gate_ids": [],
      "exposure": null
    }
  ],
  "relations": [],
  "settings_description": [
    "Leg call_2: buys 1 at-the-money call expiring on the trading date itself.",
    "The buy waits until a minute closes at least 35% above where its premium stood at the entry time.",
    "Sells once a minute closes at least 35% below what was paid.",
    "Leg put_atm: buys 1 at-the-money put expiring on the trading date itself.",
    "The buy waits until a minute closes at least 50% above where its premium stood at the entry time.",
    "Sells once a minute closes at least 35% below what was paid."
  ],
  "frozen_search_settings": []
} satisfies EvidenceWorkspace;

export const STUDY_WORKSPACE = {
  "subject_kind": "study",
  "subject_id": "os_d7b4752086147e437a3a0a56ecc980f33d3c6055",
  "evidence_identity": "study:os_d7b4752086147e437a3a0a56ecc980f33d3c6055:674175cfd8a5d8fd2467b99ac315c2f289a6161bb51ad271791ebe70f9789843:2",
  "name": "SPY PUT 25mom no stop 0945-1545 baseline 2026-10-18",
  "setup": {
    "version": 1,
    "underlying": "SPY",
    "entry": {
      "anchor": "open",
      "offset_ms": 900000
    },
    "exit": {
      "anchor": "close",
      "offset_ms": 900000
    },
    "no_reentry_after": null,
    "square_off": "partial",
    "legs": [
      {
        "leg_id": "put_atm",
        "right": "put",
        "contracts": 1,
        "expiry": {
          "kind": "same_day"
        },
        "strike": {
          "kind": "atm"
        },
        "stop_loss": null,
        "momentum": {
          "kind": "percent_up",
          "value": "25"
        },
        "reentry_on_stop": null
      }
    ],
    "slippage_percent": "0.3",
    "price_step_usd": "0.01",
    "fees": {
      "kind": "per_contract",
      "per_contract_usd": "0.65"
    },
    "starting_cash_usd": "10000"
  },
  "setup_sha256": "9105810acab8c1756f980bc45412f02a7b27d9dec164d727494b35a33841b7d6",
  "source_run_id": null,
  "study_id": "os_d7b4752086147e437a3a0a56ecc980f33d3c6055",
  "period": {
    "first_session_open_ms": 1730467800000,
    "last_session_open_ms": 1777555800000,
    "scheduled_sessions": 373,
    "observed_sessions": 4
  },
  "headline": "This search did not establish a result.",
  "replay": {
    "status": "partial",
    "explanation": "Baseline diagnostics belong to this search's development scope; they are not a full-period verdict on a different setup."
  },
  "coverage": {
    "status": "partial",
    "explanation": "Baseline: 4 observed / 373 scheduled development sessions."
  },
  "research": {
    "status": "inconclusive",
    "explanation": "Research inconclusive: missing valuation evidence."
  },
  "final": {
    "status": "not_opened",
    "explanation": "The final test was not opened."
  },
  "approval": {
    "status": "not_recorded",
    "explanation": "No Golden approval is recorded for this study."
  },
  "approvals": [],
  "relations": [],
  "settings_description": [
    "Leg put_atm: buys 1 at-the-money put expiring on the trading date itself.",
    "The buy waits until a minute closes at least 25% above where its premium stood at the entry time."
  ],
  "frozen_search_settings": [
    {
      "path": "legs.put_atm.momentum.value",
      "mode": "search",
      "values": [
        "20",
        "25",
        "30"
      ]
    }
  ]
} satisfies EvidenceWorkspace;
