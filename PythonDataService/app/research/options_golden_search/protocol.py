"""Frozen Options selection protocol.

Formula: exact setup identity plus legal quantized dimensions, dated windows,
  selection rules and physical limits define one immutable procedure.
Reference: https://github.com/tim1016/learn-ai/issues/3332.
Canonical implementation: this file.
Validated against: tests/research/options_golden_search/test_protocol_search.py.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options_backtest.setup import OptionsBacktestSetup
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Policy(FrozenModel):
    version: Literal[1] = 1
    max_drawdown_fraction: Decimal = Field(default=Decimal("0.2"), ge=0, le=1)
    min_sharpe: float = 1.0
    min_activity_per_year: int = Field(default=50, ge=1)
    min_daily_observations: int = Field(default=30, ge=2)
    min_forward_sessions: int = Field(default=30, ge=1)
    min_final_sessions: int = Field(default=30, ge=1)
    max_recovery_sessions: int | None = Field(default=None, ge=1)
    risk_free_daily_return: float = 0.0
    stress_gate: bool = False


class Limits(FrozenModel):
    evaluation_cap: int = Field(default=5000, ge=1, le=5000)
    max_execution_attempts: int = Field(default=10000, ge=1)
    max_elapsed_ms: int = Field(default=86_400_000, ge=1)
    max_snapshot_bytes: int = Field(default=10_737_418_240, ge=1)
    max_artifact_bytes: int = Field(default=1_073_741_824, ge=1)
    max_snapshot_files: int = Field(default=100000, ge=1)


class ZoomSettings(FrozenModel):
    points: int = Field(default=5, ge=3, le=9)
    refinements: int = Field(default=2, ge=0, le=4)
    passes: int = Field(default=2, ge=1, le=5)


class CostScenario(FrozenModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    slippage_multiplier: Decimal = Field(default=Decimal(1), ge=1)
    fee_multiplier: Decimal = Field(default=Decimal(1), ge=1)
    slippage_percent: Decimal | None = Field(default=None, gt=0, lt=100)
    fee_per_contract_usd: Decimal | None = Field(default=None, gt=0)

    @property
    def effective_fee_multiplier(self) -> Decimal:
        return Decimal(1) if self.fee_per_contract_usd is not None else self.fee_multiplier


class Knob(FrozenModel):
    path: str
    values: tuple[Any, ...] = Field(min_length=1, max_length=5000)
    priority: int = Field(default=5, ge=1, le=10)
    mode: Literal["search", "fixed"] = "search"

    @model_validator(mode="after")
    def distinct(self) -> Knob:
        identities = [hash_payload(value) for value in self.model_dump(mode="json")["values"]]
        if len(set(identities)) != len(identities):
            raise ValueError(f"{self.path}: candidate values must be distinct")
        if self.mode == "fixed" and len(self.values) != 1:
            raise ValueError("a fixed knob has exactly one saved value")
        if self.mode == "search" and len(self.values) < 2:
            raise ValueError("a searched knob must vary at least two values")
        return self


class Window(FrozenModel):
    key: str
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, strict=True)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS, strict=True)
    session_ms: tuple[int, ...]
    annualization_sessions: float = Field(gt=0)
    activity_floor: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> Window:
        if self.start_ms >= self.end_ms or not self.session_ms:
            raise ValueError("a scored window needs a nonempty scheduled interval")
        if any(not self.start_ms <= value < self.end_ms for value in self.session_ms):
            raise ValueError("scheduled session lies outside its half-open window")
        if any(a >= b for a, b in zip(self.session_ms, self.session_ms[1:], strict=False)):
            raise ValueError("scheduled sessions must be strictly increasing and unique")
        return self


class Fold(FrozenModel):
    index: int = Field(ge=0)
    training: Window
    forward: Window


class OptionsSearchProtocol(FrozenModel):
    version: Literal[1] = 1
    setup: OptionsBacktestSetup
    knobs: tuple[Knob, ...]
    method: Literal["zoom", "grid"] = "zoom"
    policy: Policy = Field(default_factory=Policy)
    development: Window
    final: Window
    folds: tuple[Fold, ...]
    snapshot_id: str
    identities: dict[str, str]
    budget: Limits = Field(default_factory=Limits)
    finalist_count: int = Field(default=3, ge=1, le=10)
    stress: tuple[CostScenario, ...] = ()
    zoom: ZoomSettings = Field(default_factory=ZoomSettings)
    pair_audits: tuple[tuple[str, str], ...] = ()
    calendar_ineligible: Literal["skip_counted"] = "skip_counted"
    gate_matrix_version: Literal[1] = 1

    @property
    def protocol_hash(self) -> str:
        return hash_payload(self.model_dump(mode="json"))

    @property
    def search_knobs(self) -> tuple[Knob, ...]:
        return tuple(sorted((k for k in self.knobs if k.mode == "search"), key=lambda k: (-k.priority, k.path)))

    @model_validator(mode="after")
    def consistent(self) -> OptionsSearchProtocol:
        paths = [knob.path for knob in self.knobs]
        if len(set(paths)) != len(paths) or not self.search_knobs:
            raise ValueError("declare each knob once and vary at least one")
        if any(a != b and (a.startswith(b + ".") or b.startswith(a + ".")) for a in paths for b in paths):
            raise ValueError("a rule and its child setting cannot both be dimensions")
        for knob in self.knobs:
            original = read_path(self.setup, knob.path)
            if knob.mode == "fixed" and hash_payload(original) != hash_payload(knob.model_dump(mode="json")["values"][0]):
                raise ValueError(f"{knob.path}: held settings must equal the saved value")
            for value in knob.values:
                validate_value(self.setup, knob.path, value)
        if self.final.start_ms != self.development.end_ms:
            raise ValueError("final interval must start at the development boundary")
        if len(self.folds) < 3:
            raise ValueError("at least three complete forward folds are required")
        for index, fold in enumerate(self.folds):
            if fold.index != index or fold.training.end_ms != fold.forward.start_ms:
                raise ValueError("fold indices must be contiguous and training must precede its test")
            if not self.development.start_ms <= fold.training.start_ms < fold.forward.end_ms <= self.development.end_ms:
                raise ValueError("folds must lie within development")
            if index and self.folds[index - 1].forward.end_ms != fold.forward.start_ms:
                raise ValueError("forward windows must be contiguous and disjoint")
        if self.folds[-1].forward.end_ms != self.development.end_ms:
            raise ValueError("forward folds cannot leave a development tail")
        searched = {k.path for k in self.search_knobs}
        pairs = [frozenset(pair) for pair in self.pair_audits]
        if len(set(pairs)) != len(pairs) or any(len(pair) != 2 or not pair <= searched for pair in pairs):
            raise ValueError("pair audits must name distinct searched dimensions once")
        if len({s.key for s in self.stress}) != len(self.stress) or any(s.key == "base" for s in self.stress):
            raise ValueError("stress scenario names must be unique and cannot be base")
        if self.policy.stress_gate and not self.stress:
            raise ValueError("an enabled stress gate requires at least one frozen stress scenario")
        for scenario in self.stress:
            apply_scenario(self.setup, scenario)
        return self


def _location(setup: OptionsBacktestSetup, path: str) -> tuple[dict[str, Any], list[str]]:
    data = setup.model_dump(mode="json")
    pieces = path.split(".")
    if pieces[0] == "legs" and len(pieces) >= 3:
        leg = next((item for item in data["legs"] if item["leg_id"] == pieces[1]), None)
        if leg is None:
            raise ValueError(f"unknown stable leg id: {pieces[1]}")
        if pieces[2] not in {"stop_loss", "momentum", "reentry_on_stop"} or len(pieces) > 4:
            raise ValueError(f"unsupported search field: {path}")
        if len(pieces) == 4 and pieces[3] not in ({"count", "kind"} if pieces[2] == "reentry_on_stop" else {"value", "kind"}):
            raise ValueError(f"unsupported rule field: {path}")
        return data, ["legs", str(data["legs"].index(leg)), *pieces[2:]]
    if pieces[0] in {"entry", "exit", "no_reentry_after"} and (len(pieces) == 1 or pieces[1:] == ["offset_ms"]):
        return data, pieces
    raise ValueError(f"unsupported frozen field: {path}")


def read_path(setup: OptionsBacktestSetup, path: str) -> Any:
    data, pieces = _location(setup, path)
    current: Any = data
    for piece in pieces:
        if current is None:
            raise ValueError(f"{path}: absent rule must be varied as an explicit rule category")
        current = current[int(piece)] if isinstance(current, list) else current[piece]
    return current


def _assign(data: dict[str, Any], pieces: list[str], value: Any) -> None:
    current: Any = data
    for piece in pieces[:-1]:
        current = current[int(piece)] if isinstance(current, list) else current[piece]
        if current is None:
            raise ValueError("cannot vary a child of an absent rule")
    if pieces[-1] == "count" and value == 0:
        # Zero is the canonical absence of re-entry, not an invalid count-one rule.
        parent: Any = data
        for piece in pieces[:-2]:
            parent = parent[int(piece)] if isinstance(parent, list) else parent[piece]
        parent[pieces[-2]] = None
    else:
        current[pieces[-1]] = value


def validate_value(setup: OptionsBacktestSetup, path: str, value: Any) -> None:
    """Validate scalar typing and local rule constraints without rejecting legal dependency combinations."""
    from app.options_backtest.setup import Momentum, ReentryOnStop, SessionTime, StopLoss

    data, pieces = _location(setup, path)
    field = pieces[-1]
    if field == "offset_ms" and (type(value) is not int or value < 0 or value % 60000):
        raise ValueError(f"{path}: offsets are integer whole-minute milliseconds")
    if field == "count" and (type(value) is not int or not 0 <= value <= 20):
        raise ValueError(f"{path}: re-entry count is an integer from 0 to 20")
    if field == "value":
        if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
            raise ValueError(f"{path}: exact rule values must be decimal strings or integers")
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{path}: invalid exact decimal amount") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError(f"{path}: rule value must be finite and positive")
    _assign(data, pieces, value)
    if pieces[0] == "legs":
        leg = data["legs"][int(pieces[1])]
        rule = leg[pieces[2]]
        model = {"stop_loss": StopLoss, "momentum": Momentum, "reentry_on_stop": ReentryOnStop}[pieces[2]]
        if rule is not None:
            if isinstance(rule, dict) and "value" in rule and (isinstance(rule["value"], bool) or not isinstance(rule["value"], str | int | Decimal)):
                raise ValueError(f"{path}: exact rule values must be decimal strings or integers")
            parsed = model.model_validate(rule)
            if pieces[2] == "stop_loss" and leg.get("side", "buy") == "buy" and parsed.kind == "percent" and parsed.value >= 100:
                raise ValueError("a bought percent stop must be below 100")
    elif data[pieces[0]] is not None:
        SessionTime.model_validate(data[pieces[0]])


def materialize(protocol: OptionsSearchProtocol, point: dict[str, Any]) -> OptionsBacktestSetup:
    """Apply settings by stable leg identity, then ask the canonical setup to validate dependencies."""
    declared = {k.path: k for k in protocol.knobs}
    if set(point) - set(declared):
        raise ValueError("point contains undeclared settings")
    data = protocol.setup.model_dump(mode="json")
    for knob in protocol.knobs:
        if knob.mode == "fixed":
            value = knob.values[0]
            if knob.path in point and point[knob.path] != value:
                raise ValueError("point attempts to change a fixed setting")
        elif knob.path in point:
            value = point[knob.path]
            if value not in knob.values and value != read_path(protocol.setup, knob.path):
                raise ValueError(f"{knob.path}: point leaves its frozen categories")
        else:
            continue
        _, pieces = _location(protocol.setup, knob.path)
        _assign(data, pieces, value)
    return OptionsBacktestSetup.model_validate(data)


def point_hash(setup: OptionsBacktestSetup) -> str:
    return hash_payload(setup.model_dump(mode="json"))


def apply_scenario(setup: OptionsBacktestSetup, scenario: CostScenario) -> OptionsBacktestSetup:
    """Scale canonical option fees without replacing estimated tiers with stock fees.

    Formula: scenario slippage = saved slippage * multiplier; explicit fees scale likewise.
    Reference: https://github.com/tim1016/learn-ai/issues/3332.
    Canonical implementation: this file.
    Validated against: tests/research/options_golden_search/test_protocol_search.py.
    """
    data = setup.model_dump(mode="json")
    zero_fees = setup.fees.kind == "not_charged" or (setup.fees.kind == "per_contract" and not setup.fees.per_contract_usd)
    if scenario.slippage_percent is not None and setup.slippage_percent != 0:
        raise ValueError("an absolute stress slippage amount is only for a zero base")
    if scenario.fee_per_contract_usd is not None and not zero_fees:
        raise ValueError("an absolute stress fee is only for a zero base; otherwise scale canonical fees")
    if scenario.slippage_multiplier > 1 and setup.slippage_percent == 0 and scenario.slippage_percent is None:
        raise ValueError("zero saved slippage requires an explicit positive stress amount")
    data["slippage_percent"] = scenario.slippage_percent if scenario.slippage_percent is not None else setup.slippage_percent * scenario.slippage_multiplier
    if scenario.fee_multiplier > 1 and zero_fees and scenario.fee_per_contract_usd is None:
        raise ValueError("zero saved fees require an explicit positive stress amount")
    if scenario.fee_per_contract_usd is not None:
        data["fees"] = {"kind": "per_contract", "per_contract_usd": scenario.fee_per_contract_usd}
    # The replay scales canonical fee outputs after applying tier semantics.
    return OptionsBacktestSetup.model_validate(data)


def declared_knobs(setup: OptionsBacktestSetup) -> list[dict[str, Any]]:
    """Supported paths with saved values; rule categories are explicit and independent of vary/fixed."""
    paths = ["entry.offset_ms", "exit.offset_ms", "no_reentry_after"]
    if setup.no_reentry_after is not None:
        paths.append("no_reentry_after.offset_ms")
    for leg in setup.legs:
        for name in ("stop_loss", "momentum", "reentry_on_stop"):
            path = f"legs.{leg.leg_id}.{name}"
            paths.append(path)
            rule = getattr(leg, name)
            if rule is not None:
                paths.extend([path + ".kind", path + (".count" if name == "reentry_on_stop" else ".value")])
    rows = []
    for path in paths:
        value = read_path(setup, path)
        name = path.rsplit(".", 1)[-1]
        presence = name in {"stop_loss", "momentum", "reentry_on_stop", "no_reentry_after"}
        value_type = "integer" if name in {"count", "offset_ms"} else "decimal" if name == "value" else "category"
        if presence:
            default = {"stop_loss": {"kind": "percent", "value": "10"},
                       "momentum": {"kind": "percent_up", "value": "1"},
                       "reentry_on_stop": {"kind": "at_once", "count": 1},
                       "no_reentry_after": {"anchor": "close", "offset_ms": 60000}}[name]
            legal = [None, value or default]
        elif name == "kind":
            rule = path.split(".")[-2]
            legal = {"stop_loss": ["percent", "points"], "momentum": ["percent_up", "percent_down", "points_up", "points_down"],
                     "reentry_on_stop": ["at_once", "on_momentum"]}[rule]
        else:
            legal = []
        rows.append({"path": path, "label": path.replace(".", " · ").replace("_", " "),
            "leg_id": path.split(".")[1] if path.startswith("legs.") else None,
            "current_value": value, "rule_presence": presence, "value_type": value_type,
            "legal_values": legal, "min": 0 if name in {"count", "offset_ms"} else "0.0001" if name == "value" else None,
            "max": 20 if name == "count" else 86_340_000 if name == "offset_ms" else None,
            "step": 60_000 if name == "offset_ms" else 1 if name == "count" else "0.0001" if name == "value" else None,
            "help": "Include rule: Off/On is separate from Vary in search." if presence else "Held settings retain the saved value."})
    return rows
