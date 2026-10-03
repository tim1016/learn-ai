"""What the research supports for each candidate, one row per kind of evidence, beside the Compare step (#2811).

Each row is ``meets``, ``concern`` or ``missing`` with a plain sentence and a
link to the evidence it summarizes. A result the study did not record is
``missing``, never a pass, and a row computes no new statistic: it classifies
what the stages already stored against the frozen plan's own rules (the
development and recent trade floors, the procedure's verdict, the neighbor and
stress runs, the final interval's recorded exposure). No stage measures how
much of a result comes from a few trades or months yet, so that row is always
``missing``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.compare_measures import completed_net

Status = Literal["meets", "concern", "missing"]
Window = tuple[int, int]

_VERDICT_STATUS: dict[str, Status] = {
    "still worked": "meets",
    "got worse": "concern",
    "stopped working": "concern",
    "too few trades": "concern",
    "could not be judged": "missing",
}
_EXPOSURE_STATUS: dict[str, Status] = {"not_opened": "meets", "previously_used": "concern", "history_unknown": "concern"}


def _row(key: str, label: str, status: Status, text: str, link: tuple[str, str]) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "text": text, "link": {"kind": link[0], "target": link[1]}}


def decision_summaries(
    results: Mapping[str, Any], *, floors: TradeFloors, development: Window, exposure: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Every evidence candidate's rows, in the order Compare lists the candidates; empty before the evidence exists."""
    evidence = results.get("evidence")
    if evidence is None:
        return []
    shared = [_test_over_time(results.get("validation")), _concentration(), _final_exposure(results.get("exam"), exposure)]
    summaries = []
    for item in evidence["candidates"]:
        rows = [_development_activity(item, floors.at(development))]
        if item["key"] == "recent":
            rows.append(_recent_activity(results.get("recent"), floors))
        if item["key"] != "incumbent":
            rows.append(shared[0])
        rows += [_neighbors(item), _stress(item), *shared[1:]]
        summaries.append({"candidate_key": item["key"], "rows": rows})
    return summaries


def _development_activity(item: Mapping[str, Any], floor: int) -> dict[str, Any]:
    label, link = "Development activity", ("tab", "trades")
    metrics = item["development_metrics"]
    if metrics is None:
        return _row("development_activity", label, "missing", "Not evaluated: the study's budget ran out before this candidate's development run.", link)
    if metrics["status"] != "completed":
        return _row("development_activity", label, "missing", f"The development run failed: {metrics.get('error') or 'no reason recorded'}.", link)
    trades = int(metrics["total_trades"])
    if trades >= floor:
        return _row("development_activity", label, "meets", f"{trades} trades over the development period; its minimum is {floor}.", link)
    return _row("development_activity", label, "concern", f"{trades} trades over the development period, below its minimum of {floor}.", link)


def _recent_activity(recent: Mapping[str, Any] | None, floors: TradeFloors) -> dict[str, Any]:
    label, link = "Recent activity", ("step", "search")
    if recent is None:
        return _row("recent_activity", label, "missing", "No recent-window fit was recorded.", link)
    floor = floors.at((int(recent["window"]["start_ms"]), int(recent["window"]["end_ms"])))
    procedure = recent["procedure"]
    metrics = procedure["winner_metrics"]
    if metrics is None or metrics["status"] != "completed":
        return _row("recent_activity", label, "missing", "The recent-window fit recorded no completed result.", link)
    trades = int(metrics["total_trades"])
    status: Status = "meets" if trades >= floor else "concern"
    relation = "its minimum is" if status == "meets" else "below its minimum of"
    # No setting passing every rule is not an activity finding; the trade count still is.
    seed = " No setting met every rule there, so this candidate is its starting point." if procedure["stop_reason"] == "no_eligible" else ""
    return _row("recent_activity", label, status, f"{trades} trades over the recent window; {relation} {floor}.{seed}", link)


def _test_over_time(validation: Mapping[str, Any] | None) -> dict[str, Any]:
    label, link = "Test over time", ("step", "test")
    if validation is None or validation.get("verdict") is None:
        return _row("test_over_time", label, "missing", "Test over time has not produced a verdict.", link)
    verdict = validation["verdict"]
    folds: Sequence[Mapping[str, Any]] = validation.get("folds") or []
    completed = sum(1 for fold in folds if fold.get("status") == "completed")
    status = _VERDICT_STATUS.get(verdict["label"], "missing")
    coverage = f"{completed} of {len(folds)} scheduled folds completed."
    return _row("test_over_time", label, status, f"The search procedure {verdict['label']}: {verdict['reason']}. {coverage}", link)


def _neighbors(item: Mapping[str, Any]) -> dict[str, Any]:
    label, link = "Neighbor sensitivity", ("chart", "neighbor-tornado")
    hoods: Sequence[Mapping[str, Any]] = item.get("neighbors") or []
    if not item.get("neighbors_audited") or not hoods:
        return _row("neighbors", label, "missing", "No neighbor audit was recorded for these settings.", link)
    losing: list[str] = []
    unrecorded = 0
    for hood in hoods:
        for row in hood["rows"]:
            if row["status"] in ("failed", "untested") or (row["status"] == "tested" and (row["metrics"] or {}).get("net_profit") is None):
                unrecorded += 1
            elif row["status"] == "tested" and row["metrics"]["net_profit"] < 0 and hood["knob"] not in losing:
                losing.append(hood["knob"])
    if losing:
        return _row("neighbors", label, "concern", f"A one-step change in {', '.join(losing)} loses money.", link)
    if unrecorded:
        return _row("neighbors", label, "missing", f"{unrecorded} neighbor {'run was' if unrecorded == 1 else 'runs were'} not recorded.", link)
    return _row("neighbors", label, "meets", "No tested one-step neighbor loses money.", link)


def _stress(item: Mapping[str, Any]) -> dict[str, Any]:
    label, link = "Cost stresses", ("chart", "cost-stress")
    base = item["development_metrics"]
    results: Sequence[Mapping[str, Any]] = item.get("stress") or []
    if base is None or base["status"] != "completed" or base.get("net_profit") is None:
        return _row("stress", label, "missing", "Not stressed: the development run has no completed result.", link)
    if not results:
        return _row("stress", label, "missing", "The plan stressed no costs.", link)
    if base["net_profit"] < 0:
        return _row("stress", label, "concern", "It loses money before any cost stress.", link)
    if base["net_profit"] == 0:
        return _row("stress", label, "concern", "It only breaks even before any cost stress.", link)
    nets = [(result["label"], completed_net(result.get("metrics"))) for result in results]
    losses = [scenario for scenario, net in nets if net is not None and net < 0]
    even = [scenario for scenario, net in nets if net == 0]
    unrecorded = sum(1 for _, net in nets if net is None)
    if losses or even:
        parts = ([f"{'; '.join(losses)} turns the result into a loss"] if losses else []) + ([f"{'; '.join(even)} leaves it at break-even"] if even else [])
        return _row("stress", label, "concern", f"{'. '.join(parts)}.", link)
    if unrecorded:
        return _row("stress", label, "missing", f"{unrecorded} stress {'run was' if unrecorded == 1 else 'runs were'} not recorded.", link)
    return _row("stress", label, "meets", "Every stressed run still makes money.", link)


def _concentration() -> dict[str, Any]:
    return _row(
        "concentration",
        "Concentration",
        "missing",
        "Not measured: no stage yet checks how much of the result comes from a few trades or months.",
        ("tab", "months"),
    )


def _final_exposure(exam: Mapping[str, Any] | None, preview: Mapping[str, Any] | None) -> dict[str, Any]:
    label, link = "Final-test exposure", ("step", "decision")
    state = (exam or {}).get("exposure_state") or (preview or {}).get("state")
    if state is None:
        return _row("final_exposure", label, "missing", "The final interval's recorded use was not checked.", link)
    status = _EXPOSURE_STATUS.get(state, "missing")
    if state == "not_opened":
        text = "No recorded research has used the final interval, so its one look counts as confirmatory."
    elif preview is not None and preview.get("explanation"):
        text = str(preview["explanation"])
    else:
        text = "The final interval was used before, so its result is exploratory." if state == "previously_used" else "The final interval's history is unknown, so its result is exploratory."
    return _row("final_exposure", label, status, text, link)
