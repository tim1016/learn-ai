"""Hand-written exact Options histories used by evidence and evaluator tests."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from app.options_backtest.replay import replay_session
from app.options_backtest.run import load_session_market
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import setup_sha256
from app.research import options_candidate_evidence as reader
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search import stages
from app.research.options_golden_search.marks import MARK_RULE_V2
from app.research.options_golden_search.metrics import SessionEvidence, evidence_from_json, summarize_evidence
from app.research.options_golden_search.projections import exact_data
from app.research.options_golden_search.protocol import OptionsSearchProtocol
from app.research.runs.hashing import hash_payload
from tests.options_backtest._market import leg, no_fee, setup
from tests.options_backtest._run_data import (
    FakeRunData,
    contract_on,
    flat,
    listing_on,
    option_bars,
    steady,
    underlying_bars,
)


def scene(
    days: tuple[date, ...], *, exits: tuple[str, ...], trough: str = "1", missing_minute: int | None = None,
) -> tuple[OptionsBacktestSetup, FakeRunData]:
    options = {}
    for day, exit_price in zip(days, exits, strict=True):
        bars = {**flat("1", 5, 60), 7: ("1", trough), 61: (exit_price, exit_price)}
        if missing_minute is not None:
            bars.pop(missing_minute, None)
        options[(contract_on(day, "call", "500"), day)] = option_bars(day, bars)
    return setup(leg()), FakeRunData(
        underlying={day: underlying_bars(day, steady("500")) for day in days},
        listings={day: listing_on(day) for day in days}, options=options,
    )


def session_evidence(
    day: date, *, cash: str = "10000", exit_price: str = "2", trough: str = "1", no_trade: bool = False,
) -> SessionEvidence:
    written, data = scene((day,), exits=(exit_price,), trough=trough)
    if no_trade:
        written = setup(leg(momentum=("percent_up", "1000")))
    market, _pins = load_session_market(written, day, data)
    observed = []
    outcome = replay_session(written, market, cash_usd=Decimal(cash), fee_for=no_fee, observer=observed.append)
    return SessionEvidence(market.session_open_ms, market.session_close_ms, Decimal(cash), outcome, tuple(observed))


def as_a_new_linked_study_under_mark_rule_v2(
    row: dict[str, Any], record: dict[str, Any], blobs: BlobStore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A retained study's baseline evaluation, valued under mark rule v2 as a new linked study's record would be.

    The raw retained bytes are untouched. The protocol freezes v2, which moves its hash and every evaluation key, so
    the record is the one a new study would store; the original study and its identities are not rewritten.
    """
    protocol = OptionsSearchProtocol.model_validate(row["protocol"]).model_copy(update={"mark_rule": MARK_RULE_V2})
    linked = {**row, "protocol": protocol.model_dump(mode="json"), "protocol_hash": protocol.protocol_hash}
    _, setup, window, key = reader.resolve(linked, setup_sha256(protocol.setup), "development")
    identity = {"protocol": protocol.protocol_hash, "setup": setup.model_dump(mode="json"),
        "window": window.model_dump(mode="json"), "scenario": "base", "snapshot": protocol.snapshot_id,
        "identities": protocol.identities}
    assert hash_payload(identity) == key
    sessions = tuple(evidence_from_json(item) for item in json.loads(blobs.get(record["result"]["evidence_blob"]))["sessions"])
    metrics = summarize_evidence(sessions, starting_capital_usd=protocol.setup.starting_cash_usd,
        annualization_sessions=window.annualization_sessions, risk_free_daily_return=protocol.policy.risk_free_daily_return,
        mark_rule=MARK_RULE_V2)
    result = {**record["result"], "metrics": stages.metric_summary(metrics), "metrics_sha256": hash_payload(exact_data(metrics))}
    return linked, {**record, "key": key, "identity": identity, "result": result}
