# ADR 0059 Slice 1 — Worlds and Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After this slice a real-money Alpaca account is *visible* — readable, rendered loudly as live, with its margin fields and a server-derived live verdict — while every one of the thirteen order-path gates still refuses it.

**Architecture:** Four closed account worlds replace two (`real_paper | real_live | shadow | synthetic`); `AlpacaSettings` admits `live` only with every `ALPACA_LIVE_*` value present; the adapter derives `account_mode` from the settings mode and uses the `PA` account-number shape as a refusal-only input; capabilities select by mode; seven margin fields ride the account snapshot nullable; a pure `alpaca_live_verdict()` composes settings and the clerk selection outcome into an `AlpacaLiveVerdict` served at `GET /api/brokers/alpaca/live-verdict`; a root-scoped Frontend service polls it and a top-bar banner renders it, and the two hardcoded "Paper" chrome sites become mode-driven.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2 + pydantic-settings, pytest (`asyncio_mode=auto`), httpx `ASGITransport`, ruff · Angular 22 zoneless, signals, Angular Testing Library, Vitest, PrimeNG `p-tag` · openapi-typescript codegen.

**Spec:** `docs/architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md` — Decisions 1, 7, 8, 9 and Consequences slice 1. Read it first; every task below cites the decision it implements.

## Global Constraints

- **No gate opens.** Every `account_mode != "paper"` refusal (`active_authority.py:220`, `sqlite/runtime.py:202`, `sqlite/account_operator_posture.py:323`, `sqlite/manual_order_runtime.py:114`, `sqlite/historical_execution_recovery.py:137`, `sqlite/cutover.py:126,696`, `sqlite/dev_reset.py:108`, `broker_v2_panel/panel_deploy.py:65`, `services/run_admission.py:219`) is **not touched** in this slice. Slice 7 re-means them. The five tests pinning `LIVE_ACCOUNT_REFUSED` must stay green.
- **Branch:** `feat/live-slice-1-worlds-identity` from `master` (already created; HEAD `85325f27`). Not stacked on `docs/adr-0059-real-money-live`.
- **Every `ALPACA_LIVE_*` value comes from the environment file, is required when `ALPACA_MODE=live`, and has no default in code** (ADR 0059 D4). Names, verbatim: `ALPACA_LIVE_LOSS_FRACTION`, `ALPACA_LIVE_LOSS_USD`, `ALPACA_LIVE_SHADOW_SESSIONS`, `ALPACA_LIVE_ARMING_MAX_SESSIONS`, `ALPACA_LIVE_XH_ENTRY_BPS`, `ALPACA_LIVE_XH_EXIT_BPS`.
- **The account-id shape never grants a mode; it only refuses one** (ADR 0059 D1; ADR 0054). Alpaca paper account numbers begin `PA`.
- **Time is `int64 ms UTC`** everywhere (ADR 0022). Use `now_ms_utc()` from `app.utils.timestamps`.
- **Reason codes** are `SCREAMING_SNAKE`: `LIVE_MODE_DISAGREEMENT` is the only new one in this slice.
- **Frontend renders server-authored verdict copy; it never composes a verdict from per-field facts** (ADR 0011 §7). Raw codes reach the template through `receiptLabel`.
- **Contract regeneration is part of the slice:** `PythonDataService/.venv/bin/python scripts/export_openapi_contract.py` then `cd Frontend && npm run codegen:openapi`. CI runs `--check` and `codegen:check`.
- Follow the patterns in each file; no reformatting on the way through. `from __future__ import annotations` in every Python module. Structured logging only. No `print`. No silent `except`.
- Test commands: Python `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest <path> -v`; Frontend `podman exec my-frontend npx ng test --include='<exact spec path>'` (never a directory glob). Lint before push: `ruff check PythonDataService/app/ PythonDataService/tests/` and `npx eslint Frontend/src/ --max-warnings 0`.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File Structure

**Python — modify**
- `PythonDataService/app/broker/alpaca/clerk/account_authority.py` — the four worlds; `shadow:` namespace; rename.
- `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:20,205` — rename call site.
- `PythonDataService/app/schemas/account_authority.py` — widen `AuthorityKind`; namespace validator.
- `PythonDataService/app/schemas/broker_v2_panel.py:41,399,424` · `PythonDataService/app/schemas/clerk_custody.py:63` · `PythonDataService/app/broker/alpaca/clerk/models.py` (`ClerkStatus.authority_kind`) — widen the closed pair.
- `PythonDataService/app/broker/alpaca/config.py` — `_enforce_mode_agreement`, `ALPACA_LIVE_*` fields, `is_live`.
- `PythonDataService/app/broker/contract/errors.py` — `BrokerAccountModeDisagreement`.
- `PythonDataService/app/broker/contract/models.py:136-154` — seven nullable margin fields.
- `PythonDataService/app/broker/alpaca/adapter.py:172-197` — derive mode, shape refusal, margin fields.
- `PythonDataService/app/broker/alpaca/broker.py:38-75` — `ALPACA_LIVE_CAPABILITIES`, select by mode, pass mode to the adapter.
- `PythonDataService/app/broker/alpaca/clerk/active_authority.py:206-225` — catch the disagreement as `LIVE_MODE_DISAGREEMENT`.
- `PythonDataService/app/routers/brokers.py` — `GET /{broker}/live-verdict`.
- `.env.example` — the six names, no values.

**Python — create**
- `PythonDataService/app/schemas/alpaca_live_verdict.py` — `AlpacaLiveVerdict`.
- `PythonDataService/app/services/alpaca_live_verdict.py` — `alpaca_live_verdict()` pure composer.

**Python — tests (modify / create)**
- `PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py` (create)
- `PythonDataService/tests/broker/alpaca/clerk/test_account_keyed_authority.py:163-190` (extend parametrization)
- `PythonDataService/tests/broker/alpaca/test_config.py` (modify)
- `PythonDataService/tests/broker/alpaca/test_adapter_account.py` (modify + extend)
- `PythonDataService/tests/broker/alpaca/test_capabilities.py` (modify)
- `PythonDataService/tests/services/test_alpaca_live_verdict.py` (create)
- `PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py` (create)

**Frontend — modify**
- `Frontend/src/app/api/alpaca.types.ts` — alias `AlpacaLiveVerdict`.
- `Frontend/src/app/services/brokers.service.ts` — `getLiveVerdict()`.
- `Frontend/src/app/app.component.ts` — import, template slot, `start()`.
- `Frontend/src/app/components/broker/v2-panel/account-strip/account-strip.component.html:8,19` + `.scss:70` — mode-driven badge, neutral skeleton copy.
- `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.html:14,20-37` — mode-driven tag, margin rows.

**Frontend — create**
- `Frontend/src/app/services/alpaca-live-verdict.service.ts` + `.spec.ts` — root poller modelled on `BrokerHealthService`.
- `Frontend/src/app/shell/alpaca-live-banner.component.ts` + `.spec.ts` — the top-bar banner.

**Generated**
- `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts`.

---

### Task 1: Four account worlds and the `shadow:` namespace (ADR 0059 D1)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/account_authority.py`
- Modify: `PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py:20,205`
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py:145-156` (no logic change; type widens through the import)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py` (create)

**Interfaces:**
- Produces: `AccountAuthorityKind = Literal["real_paper", "real_live", "shadow", "synthetic"]`; `SHADOW_ACCOUNT_PREFIX = "shadow:"`; `is_shadow_account_id(account_id: str) -> bool`; `require_real_account_id(account_id: str) -> str` (rejects `sim:` and `shadow:`); `require_shadow_account_id(account_id: str) -> str`; `authority_kind_for_account(account_id: str, *, account_mode: Literal["paper", "live"] = "paper") -> AccountAuthorityKind`; `shadow_account_id_for_live_account(live_account_id: str) -> str`. `require_real_paper_account_id` is **removed** (not aliased) so a stale import fails loudly.

- [ ] **Step 1: Write the failing tests**

```python
# PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py
"""The four closed account worlds (ADR 0059 D1).

``sim:`` and ``shadow:`` are reserved namespaces; a real Alpaca port binds to
neither. ``real_live`` is never inferred from an account id — it is the
caller's positively-learned mode, so the kind derivation takes it as input.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.account_authority import (
    SHADOW_ACCOUNT_PREFIX,
    AccountAuthorityIdentityError,
    authority_kind_for_account,
    is_shadow_account_id,
    require_real_account_id,
    require_shadow_account_id,
    shadow_account_id_for_live_account,
)


def test_shadow_prefix_is_reserved_and_distinct_from_sim() -> None:
    assert SHADOW_ACCOUNT_PREFIX == "shadow:"
    assert is_shadow_account_id("shadow:9LIVE0001") is True
    assert is_shadow_account_id("sim:ema-1") is False
    assert is_shadow_account_id("PA0SANITIZED00001") is False


@pytest.mark.parametrize("account_id", ["sim:ema-1", "shadow:9LIVE0001"])
def test_real_ports_refuse_both_reserved_namespaces(account_id: str) -> None:
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        require_real_account_id(account_id)


def test_real_account_id_passes_paper_and_live_shapes() -> None:
    assert require_real_account_id("PA0SANITIZED00001") == "PA0SANITIZED00001"
    assert require_real_account_id("9LIVE0001") == "9LIVE0001"


def test_shadow_requires_the_shadow_namespace_and_names_one_account() -> None:
    assert require_shadow_account_id("shadow:9LIVE0001") == "shadow:9LIVE0001"
    with pytest.raises(AccountAuthorityIdentityError, match="shadow: account identity"):
        require_shadow_account_id("PA0SANITIZED00001")
    with pytest.raises(AccountAuthorityIdentityError, match="name one account"):
        require_shadow_account_id("shadow:")


def test_authority_kind_takes_mode_as_input_never_from_shape() -> None:
    assert authority_kind_for_account("sim:ema-1") == "synthetic"
    assert authority_kind_for_account("shadow:9LIVE0001") == "shadow"
    assert authority_kind_for_account("PA0SANITIZED00001") == "real_paper"
    # A live-shaped id under the default (paper) mode is still real_paper: the
    # shape never grants live. Only the caller's positive mode does.
    assert authority_kind_for_account("9LIVE0001") == "real_paper"
    assert authority_kind_for_account("9LIVE0001", account_mode="live") == "real_live"


def test_shadow_account_id_derives_from_the_live_account() -> None:
    assert shadow_account_id_for_live_account("9LIVE0001") == "shadow:9LIVE0001"
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        shadow_account_id_for_live_account("sim:ema-1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_worlds.py -v`
Expected: FAIL — `ImportError: cannot import name 'SHADOW_ACCOUNT_PREFIX'`.

- [ ] **Step 3: Implement the worlds**

Replace the top of `account_authority.py` (from the module docstring through `synthetic_account_id_for_strategy`) with:

```python
"""Account identity and port binding for Clerk authorities.

An authority is selected by its account identity, never by whichever Clerk was
constructed last in this process.  ``sim:`` and ``shadow:`` are closed
namespaces (ADR 0059 D1): a real Alpaca port cannot be bound to either, a
synthetic port cannot be bound to an external account, and a shadow authority
reads one real-money account while never submitting to it.  ``real_live`` is
never inferred from an account id's shape — it is the caller's positively
learned mode (ADR 0054), so the kind derivation takes it as an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

AccountAuthorityKind = Literal["real_paper", "real_live", "shadow", "synthetic"]
SIM_ACCOUNT_PREFIX = "sim:"
SHADOW_ACCOUNT_PREFIX = "shadow:"
_RESERVED_PREFIXES: tuple[str, ...] = (SIM_ACCOUNT_PREFIX, SHADOW_ACCOUNT_PREFIX)


class AccountAuthorityIdentityError(ValueError):
    """A port or authority was given an account from the other namespace."""


def is_synthetic_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` belongs to the reserved synthetic namespace."""
    return account_id.startswith(SIM_ACCOUNT_PREFIX)


def is_shadow_account_id(account_id: str) -> bool:
    """Return whether ``account_id`` belongs to the reserved shadow namespace."""
    return account_id.startswith(SHADOW_ACCOUNT_PREFIX)


def require_real_account_id(account_id: str) -> str:
    """Reject a reserved-namespace identity before it can bind an Alpaca port."""
    if not isinstance(account_id, str) or not account_id:
        raise AccountAuthorityIdentityError("real account identity must be non-empty")
    if account_id.startswith(_RESERVED_PREFIXES):
        raise AccountAuthorityIdentityError(
            "real Alpaca ports refuse reserved sim:/shadow: account identities"
        )
    return account_id


def require_synthetic_account_id(account_id: str) -> str:
    """Reject an external account before it can bind a simulated port."""
    if not isinstance(account_id, str) or not account_id.startswith(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic ports require a sim: account identity")
    if len(account_id) == len(SIM_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("synthetic account identity must name one account")
    return account_id


def require_shadow_account_id(account_id: str) -> str:
    """Reject anything but the reserved shadow namespace."""
    if not isinstance(account_id, str) or not account_id.startswith(SHADOW_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("shadow authorities require a shadow: account identity")
    if len(account_id) == len(SHADOW_ACCOUNT_PREFIX):
        raise AccountAuthorityIdentityError("shadow account identity must name one account")
    return account_id


def authority_kind_for_account(
    account_id: str,
    *,
    account_mode: Literal["paper", "live"] = "paper",
) -> AccountAuthorityKind:
    """Derive the closed read-contract kind from a namespace and a learned mode.

    ``account_mode`` is what the caller positively learned from the broker for
    a real account; it is never guessed from the id's shape.
    """
    if is_synthetic_account_id(account_id):
        return "synthetic"
    if is_shadow_account_id(account_id):
        return "shadow"
    return "real_live" if account_mode == "live" else "real_paper"


def synthetic_account_id_for_strategy(strategy_instance_id: str) -> str:
    """Return the one isolated Dry Run authority for an immutable instance."""
    from app.engine.live.identity import validate_strategy_instance_id

    return f"{SIM_ACCOUNT_PREFIX}{validate_strategy_instance_id(strategy_instance_id)}"


def shadow_account_id_for_live_account(live_account_id: str) -> str:
    """Return the one shadow authority that reads ``live_account_id``."""
    return f"{SHADOW_ACCOUNT_PREFIX}{require_real_account_id(live_account_id)}"
```

Then in `bind_real_alpaca_ports`, replace `account_id=require_real_paper_account_id(account_id)` with `account_id=require_real_account_id(account_id)` and its docstring with `"""Create a real-account composition only after rejecting reserved namespaces."""`. Update `__all__`: remove `"require_real_paper_account_id"`, add `"SHADOW_ACCOUNT_PREFIX"`, `"is_shadow_account_id"`, `"require_real_account_id"`, `"require_shadow_account_id"`, `"shadow_account_id_for_live_account"` (keep alphabetical order as the file does).

In `sqlite/runtime.py` line 20 change the import name to `require_real_account_id` and line 205 to `require_real_account_id(repo.account_id)`.

- [ ] **Step 4: Run the new tests and the existing authority tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_worlds.py tests/broker/alpaca/clerk/test_account_keyed_authority.py tests/broker/alpaca/clerk/test_authority_isolation.py -v`
Expected: PASS. (`test_account_keyed_authority.py:35` asserts `bind_real_alpaca_ports(account_id="sim:ema-1", ...)` raises `AccountAuthorityIdentityError` — still true.)

- [ ] **Step 5: Grep for the removed name**

Run: `grep -rn "require_real_paper_account_id" PythonDataService/app PythonDataService/tests`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/account_authority.py PythonDataService/app/broker/alpaca/clerk/sqlite/runtime.py PythonDataService/tests/broker/alpaca/clerk/test_account_worlds.py
git commit -m "feat(alpaca): four closed account worlds — real_live and shadow: join real_paper and sim: (ADR 0059 D1)" -m "require_real_paper_account_id never checked paper; it is now require_real_account_id and refuses both reserved namespaces. authority_kind_for_account takes the learned mode as input; shape never grants live." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Widen the wire-level `authority_kind` pair (ADR 0059 D1)

**Files:**
- Modify: `PythonDataService/app/schemas/account_authority.py`
- Modify: `PythonDataService/app/schemas/broker_v2_panel.py:41,399,424`
- Modify: `PythonDataService/app/schemas/clerk_custody.py:63`
- Modify: `PythonDataService/app/broker/alpaca/clerk/models.py` (`ClerkStatus.authority_kind`)
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_account_keyed_authority.py:163-190` (extend)

**Interfaces:**
- Produces: `app.schemas.account_authority.AuthorityKind = Literal["real_paper", "real_live", "shadow", "synthetic"]`; `_validate_account_authority` accepts `real_paper` **or** `real_live` for a real id, requires `shadow` for `shadow:`, `synthetic` for `sim:`.

- [ ] **Step 1: Extend the failing parametrized test**

In `test_account_keyed_authority.py`, find the parametrize block at lines 164-179 (the one whose first case is `("sim:ema-1", "real_paper")`) and add cases so it reads:

```python
@pytest.mark.parametrize(
    ("account_id", "authority_kind"),
    [
        ("sim:ema-1", "real_paper"),
        ("sim:ema-1", "real_live"),
        ("sim:ema-1", "shadow"),
        ("shadow:9LIVE0001", "real_paper"),
        ("shadow:9LIVE0001", "real_live"),
        ("shadow:9LIVE0001", "synthetic"),
        ("PA-REAL", "synthetic"),
        ("PA-REAL", "shadow"),
    ],
)
```

(Keep the existing case that was already there if it is not `("sim:ema-1", "real_paper")`; the assertion body — `pytest.raises(ValidationError, match="require")` — is unchanged.) Then add, after that test:

```python
@pytest.mark.parametrize(
    ("account_id", "authority_kind"),
    [
        ("PA-REAL", "real_paper"),
        ("9LIVE0001", "real_live"),
        ("shadow:9LIVE0001", "shadow"),
        ("sim:ema-1", "synthetic"),
    ],
)
def test_every_world_accepts_its_own_namespace(account_id: str, authority_kind: str) -> None:
    row = AuthorityScopedRow(account_id=account_id, authority_kind=authority_kind)  # type: ignore[arg-type]

    assert row.authority_kind == authority_kind
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_keyed_authority.py -v -k "namespace or require"`
Expected: FAIL — the `real_live`/`shadow` cases raise `ValidationError` on the `Literal`, not on the namespace message.

- [ ] **Step 3: Widen the schemas**

`app/schemas/account_authority.py` — replace the `AuthorityKind` line and `_validate_account_authority`:

```python
AuthorityKind = Literal["real_paper", "real_live", "shadow", "synthetic"]


def _validate_account_authority(account_id: str, authority_kind: AuthorityKind) -> None:
    """Keep account namespace and authority kind inseparable at the wire boundary."""
    if account_id.startswith("sim:"):
        if authority_kind != "synthetic":
            raise ValueError("sim: account ids require synthetic authority")
        return
    if account_id.startswith("shadow:"):
        if authority_kind != "shadow":
            raise ValueError("shadow: account ids require shadow authority")
        return
    if authority_kind not in ("real_paper", "real_live"):
        raise ValueError("real account ids require real_paper or real_live authority")
```

`app/schemas/broker_v2_panel.py` — at lines 41, 399 and 424 change `Literal["real_paper", "synthetic"]` to `Literal["real_paper", "real_live", "shadow", "synthetic"]`.

`app/schemas/clerk_custody.py:63` — `authority_kind: Literal["real_paper", "real_live", "shadow", "synthetic"] = "real_paper"`.

`app/broker/alpaca/clerk/models.py` — in `ClerkStatus`, `authority_kind: Literal["real_paper", "real_live", "shadow", "synthetic"] = "real_paper"`; extend the comment above it: `# Consumers must never blend rows from these four account worlds.`

- [ ] **Step 4: Run the tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_account_keyed_authority.py tests/broker/alpaca/clerk/test_authority_isolation.py tests/schemas -v -k "authority or account_keyed or isolation"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/schemas/account_authority.py PythonDataService/app/schemas/broker_v2_panel.py PythonDataService/app/schemas/clerk_custody.py PythonDataService/app/broker/alpaca/clerk/models.py PythonDataService/tests/broker/alpaca/clerk/test_account_keyed_authority.py
git commit -m "feat(alpaca): the wire authority_kind admits real_live and shadow (ADR 0059 D1)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Settings — mode agreement and the required `ALPACA_LIVE_*` values (ADR 0059 D1, D4)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/config.py:55-80`
- Modify: `.env.example`
- Test: `PythonDataService/tests/broker/alpaca/test_config.py`

**Interfaces:**
- Produces on `AlpacaSettings`: `live_loss_fraction: float | None`, `live_loss_usd: float | None`, `live_shadow_sessions: int | None`, `live_arming_max_sessions: int | None`, `live_xh_entry_bps: float | None`, `live_xh_exit_bps: float | None` (env names `ALPACA_LIVE_LOSS_FRACTION` … via the existing `env_prefix="ALPACA_"`); `is_live: bool`; validator `_enforce_mode_agreement` raising `ValueError("ALPACA_MODE=live requires <missing names>; …")`. `is_paper` and `base_url` unchanged.

- [ ] **Step 1: Rewrite the failing tests**

Replace `test_live_mode_is_refused` and update the log-assertion test in `test_config.py`:

```python
_LIVE_REQUIRED = {
    "live_loss_fraction": 0.02,
    "live_loss_usd": 500.0,
    "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20,
    "live_xh_entry_bps": 10.0,
    "live_xh_exit_bps": 10.0,
}


def test_live_mode_without_every_required_value_is_refused() -> None:
    with pytest.raises(ValidationError, match="ALPACA_MODE=live requires") as info:
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live")

    message = str(info.value)
    for name in (
        "ALPACA_LIVE_LOSS_FRACTION",
        "ALPACA_LIVE_LOSS_USD",
        "ALPACA_LIVE_SHADOW_SESSIONS",
        "ALPACA_LIVE_ARMING_MAX_SESSIONS",
        "ALPACA_LIVE_XH_ENTRY_BPS",
        "ALPACA_LIVE_XH_EXIT_BPS",
    ):
        assert name in message


def test_live_mode_names_only_the_missing_values() -> None:
    partial = dict(_LIVE_REQUIRED)
    partial.pop("live_arming_max_sessions")

    with pytest.raises(ValidationError, match="ALPACA_LIVE_ARMING_MAX_SESSIONS") as info:
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **partial)

    assert "ALPACA_LIVE_LOSS_FRACTION" not in str(info.value)


def test_live_mode_with_every_required_value_derives_live_base_url() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **_LIVE_REQUIRED)

    assert settings.is_live is True
    assert settings.is_paper is False
    assert settings.base_url == "https://api.alpaca.markets"


def test_paper_mode_ignores_live_values_entirely() -> None:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")

    assert settings.is_live is False
    assert settings.live_loss_fraction is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [("live_loss_fraction", 0.0), ("live_loss_fraction", 1.0), ("live_loss_usd", 0.0),
     ("live_shadow_sessions", 0), ("live_arming_max_sessions", 0), ("live_xh_entry_bps", -1.0)],
)
def test_live_values_have_domain_bounds(field: str, bad: float | int) -> None:
    values = dict(_LIVE_REQUIRED)
    values[field] = bad

    with pytest.raises(ValidationError):
        AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **values)
```

In `test_invalid_configuration_clears_stale_clerk_without_logging_secrets`, change the assertion `assert "ALPACA_MODE must be 'paper'" in rendered` to `assert "ALPACA_MODE=live requires" in rendered`. Keep everything else in that test as is (it still sets `ALPACA_MODE=live` with no `ALPACA_LIVE_*`, which is now the refusal path).

- [ ] **Step 2: Run to verify they fail**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_config.py -v`
Expected: FAIL — `ValidationError` messages say `ALPACA_MODE must be 'paper'`; `is_live` / `live_loss_fraction` do not exist.

- [ ] **Step 3: Implement the settings**

In `config.py`, replace the block from `mode: Literal["paper", "live"] = "paper"` through the end of `_enforce_paper_only` with:

```python
    # paper | live. ADR 0059 D1: live is admitted only on mode agreement —
    # here, that every ALPACA_LIVE_* value is present. The activation record
    # and the broker-observed mode are checked where an authority is
    # constructed (select_active_clerk_runtime), never here.
    mode: Literal["paper", "live"] = "paper"
    clerk_dir: Path = _SERVICE_ROOT / "artifacts" / "alpaca_clerk"

    # The live envelope and ceremony values (ADR 0059 D4). Required when
    # ``mode == "live"``; deliberately no defaults in code — a number nobody
    # chose must never bound real money. Sealed into the arming record by
    # slice 6; read here so the service refuses to boot live without them.
    live_loss_fraction: float | None = Field(default=None, gt=0, lt=1)
    live_loss_usd: float | None = Field(default=None, gt=0)
    live_shadow_sessions: int | None = Field(default=None, ge=1)
    live_arming_max_sessions: int | None = Field(default=None, ge=1)
    live_xh_entry_bps: float | None = Field(default=None, ge=0)
    live_xh_exit_bps: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _enforce_mode_agreement(self) -> AlpacaSettings:
        if self.mode != "live":
            return self
        missing = [
            f"ALPACA_{name.upper()}"
            for name in _LIVE_REQUIRED_FIELDS
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                f"ALPACA_MODE=live requires {', '.join(missing)}; every live envelope "
                "value comes from the environment file and none has a default "
                "(ADR 0059 D4). Refusing to start."
            )
        return self

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
```

Add, above the class (after `_BASE_URL_BY_MODE`):

```python
# The envelope and ceremony settings a live boot must carry (ADR 0059 D4).
_LIVE_REQUIRED_FIELDS: tuple[str, ...] = (
    "live_loss_fraction",
    "live_loss_usd",
    "live_shadow_sessions",
    "live_arming_max_sessions",
    "live_xh_entry_bps",
    "live_xh_exit_bps",
)
```

Update the module docstring's item 1 to: `1. ``ALPACA_MODE`` selects paper/live; the default is ``paper``. ``live`` is admitted only when every ``ALPACA_LIVE_*`` envelope value is present (ADR 0059 D1/D4); a validator raises otherwise, so a half-configured live mode cannot start the service.` and in `get_alpaca_settings`'s docstring change "validates paper-only safety" to "validates mode agreement".

Append to `.env.example`, after the `ALPACA_QUALIFICATION_*` lines:

```
# Real-money Alpaca Live (ADR 0059). Required when ALPACA_MODE=live; every
# value is deliberately absent by default — nothing in code bounds real money.
# ALPACA_MODE=paper
# ALPACA_LIVE_LOSS_FRACTION=
# ALPACA_LIVE_LOSS_USD=
# ALPACA_LIVE_SHADOW_SESSIONS=
# ALPACA_LIVE_ARMING_MAX_SESSIONS=
# ALPACA_LIVE_XH_ENTRY_BPS=
# ALPACA_LIVE_XH_EXIT_BPS=
```

- [ ] **Step 4: Run the tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_config.py tests/broker/alpaca/test_client.py -v`
Expected: PASS (`test_client.py` constructs settings for paper; unaffected).

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/broker/alpaca/config.py PythonDataService/tests/broker/alpaca/test_config.py .env.example
git commit -m "feat(alpaca): ALPACA_MODE=live is admitted only with every ALPACA_LIVE_* value present (ADR 0059 D1/D4)" -m "The paper-only validator becomes mode agreement. Six envelope values are required when live and have no defaults in code." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The adapter derives the mode, refuses a disagreeing shape, and ingests margin fields (ADR 0059 D1, D7)

**Files:**
- Modify: `PythonDataService/app/broker/contract/errors.py` (append after `BrokerSubmissionHeld`)
- Modify: `PythonDataService/app/broker/contract/models.py:136-154`
- Modify: `PythonDataService/app/broker/alpaca/adapter.py:172-197`
- Modify: `PythonDataService/app/broker/alpaca/broker.py:73-75`
- Test: `PythonDataService/tests/broker/alpaca/test_adapter_account.py`

**Interfaces:**
- Consumes: `AlpacaSettings.mode` (Task 3).
- Produces: `BrokerAccountModeDisagreement(BrokerError)` with `http_status = 409` and `reason_code = "LIVE_MODE_DISAGREEMENT"`; `from_alpaca_account(payload, *, account_mode: Literal["paper", "live"], observed_at_ms: int | None = None) -> BrokerAccountSnapshot`; seven new optional fields on `BrokerAccountSnapshot`: `multiplier: float | None = None`, `regt_buying_power: float | None = None`, `daytrading_buying_power: float | None = None`, `maintenance_margin: float | None = None`, `initial_margin: float | None = None`, `sma: float | None = None`, `last_equity: float | None = None`.

- [ ] **Step 1: Update and extend the failing tests**

In `test_adapter_account.py`, every existing `from_alpaca_account(...)` call gains `account_mode="paper"`. Extend `test_from_alpaca_account_maps_every_field` with the margin assertions (the sanitized fixture carries all but `daytrading_buying_power`), and add three tests:

```python
    # Margin fields are ingested to prove the cash bound, never to use it
    # (ADR 0059 D7). Values are the sanitized fixture's own.
    assert snapshot.multiplier == 4.0
    assert snapshot.regt_buying_power == 200000.0
    assert snapshot.daytrading_buying_power is None  # absent from the fixture
    assert snapshot.maintenance_margin == 0.0
    assert snapshot.initial_margin == 0.0
    assert snapshot.sma == 100000.0
    assert snapshot.last_equity == 100000.0
```

(These are the sanitized fixture's literal values, verified 2026-09-07: `multiplier` `'4'`, `regt_buying_power` `'200000'`, `maintenance_margin` `'0'`, `initial_margin` `'0'`, `sma` `'100000'`, `last_equity` `'100000'`; `daytrading_buying_power` is absent from the fixture and present in the SDK model.)

```python
def test_live_mode_maps_live_and_a_non_pa_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = "9LIVE0001"

    snapshot = from_alpaca_account(payload, account_mode="live", observed_at_ms=_OBSERVED)

    assert snapshot.account_mode == "live"
    assert snapshot.account_id == "9LIVE0001"


def test_live_mode_refuses_a_paper_shaped_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("account", "account.json")  # PA0SANITIZED00001

    with pytest.raises(BrokerAccountModeDisagreement) as info:
        from_alpaca_account(payload, account_mode="live", observed_at_ms=_OBSERVED)

    assert info.value.reason_code == "LIVE_MODE_DISAGREEMENT"
    assert info.value.http_status == 409
    assert "PA" in (info.value.detail or "")


def test_paper_mode_refuses_a_live_shaped_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = "9LIVE0001"

    with pytest.raises(BrokerAccountModeDisagreement):
        from_alpaca_account(payload, account_mode="paper", observed_at_ms=_OBSERVED)
```

Add the import: `from app.broker.contract.errors import BrokerAccountModeDisagreement`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_adapter_account.py -v`
Expected: FAIL — `ImportError` on `BrokerAccountModeDisagreement`, then `TypeError: unexpected keyword 'account_mode'`.

- [ ] **Step 3: Implement**

`contract/errors.py` — append:

```python
class BrokerAccountModeDisagreement(BrokerError):
    """The configured mode and the broker-observed account disagree (ADR 0059 D1).

    Raised at the ingestion boundary when an account-number shape contradicts
    the mode that selected the endpoint. Shape never *grants* a mode; it only
    refuses one. Surfaced as ``409`` with ``reason_code`` so the desk can name
    the disagreement rather than a generic broker error.
    """

    http_status: ClassVar[int] = 409
    reason_code: ClassVar[str] = "LIVE_MODE_DISAGREEMENT"
```

`contract/models.py` — in `BrokerAccountSnapshot`, after `short_market_value: float`, add:

```python
    # Margin facts, ingested so an operator can see exposure sits under cash
    # (ADR 0059 D7). Nullable: absence is unknown. The live envelope reads
    # ``cash`` and never any of these.
    multiplier: float | None = None
    regt_buying_power: float | None = None
    daytrading_buying_power: float | None = None
    maintenance_margin: float | None = None
    initial_margin: float | None = None
    sma: float | None = None
    last_equity: float | None = None
```

`adapter.py` — replace `from_alpaca_account` (lines 172-197) with:

```python
_PAPER_ACCOUNT_NUMBER_PREFIX = "PA"


def from_alpaca_account(
    payload: Mapping[str, Any],
    *,
    account_mode: Literal["paper", "live"],
    observed_at_ms: int | None = None,
) -> BrokerAccountSnapshot:
    """Map a raw Alpaca account payload to a ``BrokerAccountSnapshot``.

    ``account_mode`` is the settings mode that selected the endpoint — backend
    configuration truth, never inferred from the payload (ADR 0059 D1). The
    account-number shape is a refusal input only: a paper-shaped number under
    ``live``, or a live-shaped number under ``paper``, is a disagreement.
    """
    account_number = str(payload["account_number"])
    looks_paper = account_number.startswith(_PAPER_ACCOUNT_NUMBER_PREFIX)
    if looks_paper != (account_mode == "paper"):
        raise BrokerAccountModeDisagreement(
            "The configured Alpaca mode and the observed account disagree.",
            broker=BROKER_ID,
            detail=(
                f"ALPACA_MODE={account_mode!r} but the account number "
                f"{'begins' if looks_paper else 'does not begin'} with "
                f"{_PAPER_ACCOUNT_NUMBER_PREFIX!r}, which is the paper-account shape."
            ),
        )
    return BrokerAccountSnapshot(
        broker=BROKER_ID,
        account_id=account_number,
        account_mode=account_mode,
        account_status=str(payload["status"]),
        currency=str(payload.get("currency") or "USD"),
        cash=to_float(payload["cash"]),
        equity=to_float(payload["equity"]),
        buying_power=to_float(payload["buying_power"]),
        portfolio_value=to_float(payload["portfolio_value"]),
        long_market_value=to_float(payload["long_market_value"]),
        short_market_value=to_float(payload["short_market_value"]),
        multiplier=opt_float(payload.get("multiplier")),
        regt_buying_power=opt_float(payload.get("regt_buying_power")),
        daytrading_buying_power=opt_float(payload.get("daytrading_buying_power")),
        maintenance_margin=opt_float(payload.get("maintenance_margin")),
        initial_margin=opt_float(payload.get("initial_margin")),
        sma=opt_float(payload.get("sma")),
        last_equity=opt_float(payload.get("last_equity")),
        pattern_day_trader=opt_bool(payload.get("pattern_day_trader")),
        trading_blocked=to_bool(payload["trading_blocked"]),
        account_blocked=to_bool(payload["account_blocked"]),
        created_at_ms=opt_rfc3339_to_ms(payload.get("created_at")),
        observed_at_ms=_observed(observed_at_ms),
    )
```

Add to `adapter.py` imports: `from typing import Any, Literal` (extend the existing `typing` import) and `from app.broker.contract.errors import BrokerAccountModeDisagreement`.

`broker.py` — `get_account` becomes:

```python
    async def get_account(self) -> BrokerAccountSnapshot:
        payload = await self._client.get_account()
        # The mode that selected the endpoint is the only source of the
        # account's mode (ADR 0059 D1); the adapter refuses a disagreeing shape.
        return adapter.from_alpaca_account(payload, account_mode=get_alpaca_settings().mode)
```

and extend the import `from app.broker.alpaca.config import BROKER_ID, get_alpaca_settings`.

- [ ] **Step 4: Run the adapter tests and every test that reads an account**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_adapter_account.py tests/broker/alpaca/test_schema_drift.py tests/broker/alpaca/test_portfolio_history_endpoint.py tests/broker/alpaca/test_client.py tests/broker/v2panel/test_evidence_router.py -v`
Expected: PASS. (`grep -rn "from_alpaca_account(" PythonDataService/app PythonDataService/tests` must show only call sites that pass `account_mode=`.)

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/broker/contract/errors.py PythonDataService/app/broker/contract/models.py PythonDataService/app/broker/alpaca/adapter.py PythonDataService/app/broker/alpaca/broker.py PythonDataService/tests/broker/alpaca/test_adapter_account.py
git commit -m "feat(alpaca): the adapter derives account_mode from settings, refuses a disagreeing shape, and ingests margin fields (ADR 0059 D1/D7)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Capabilities select by mode (ADR 0059 D9)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/broker.py:33-71`
- Test: `PythonDataService/tests/broker/alpaca/test_capabilities.py`

**Interfaces:**
- Produces: `ALPACA_LIVE_CAPABILITIES: BrokerCapabilities` (`paper_only=False`, otherwise identical to paper); `AlpacaBroker.capabilities()` returns by `get_alpaca_settings().mode`.

- [ ] **Step 1: Extend the failing tests**

Rewrite `test_capabilities.py`'s imports and add a mode-selection test; parametrize the two existing tests over both descriptors:

```python
from unittest.mock import MagicMock

import pytest

from app.broker.alpaca.broker import (
    ALPACA_LIVE_CAPABILITIES,
    ALPACA_PAPER_CAPABILITIES,
    AlpacaBroker,
)
from app.broker.alpaca.config import AlpacaSettings
from app.broker.contract.models import BrokerOrderLeg, OrderType

_DESCRIPTORS = [ALPACA_PAPER_CAPABILITIES, ALPACA_LIVE_CAPABILITIES]


@pytest.mark.parametrize("capabilities", _DESCRIPTORS)
def test_advertised_order_types_match_the_constructible_enum(capabilities) -> None:
    buildable = {member.value for member in OrderType}

    assert set(capabilities.supported_order_types) == buildable


@pytest.mark.parametrize("capabilities", _DESCRIPTORS)
def test_every_advertised_order_type_builds_a_valid_leg(capabilities) -> None:
    for order_type in capabilities.supported_order_types:
        kwargs: dict[str, object] = {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "order_type": order_type,
        }
        if order_type == "limit":
            kwargs["limit_price"] = 100.0

        BrokerOrderLeg(**kwargs)


def test_live_descriptor_differs_from_paper_only_in_paper_only() -> None:
    assert ALPACA_PAPER_CAPABILITIES.paper_only is True
    assert ALPACA_LIVE_CAPABILITIES.paper_only is False
    assert ALPACA_LIVE_CAPABILITIES.model_dump(exclude={"paper_only"}) == (
        ALPACA_PAPER_CAPABILITIES.model_dump(exclude={"paper_only"})
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("paper", ALPACA_PAPER_CAPABILITIES), ("live", ALPACA_LIVE_CAPABILITIES)],
)
def test_capabilities_select_by_settings_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected
) -> None:
    live_values = {
        "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_shadow_sessions": 5,
        "live_arming_max_sessions": 20, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
    }
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode=mode, **(live_values if mode == "live" else {}))
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", lambda: settings)

    assert AlpacaBroker(client=MagicMock()).capabilities() is expected
```

(`BrokerCapabilities` is a frozen Pydantic model — `app/broker/contract/capabilities.py:15,22` — so `model_dump(exclude=...)` is correct.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_capabilities.py -v`
Expected: FAIL — `ImportError: cannot import name 'ALPACA_LIVE_CAPABILITIES'`.

- [ ] **Step 3: Implement**

In `broker.py`, directly after the `ALPACA_PAPER_CAPABILITIES = BrokerCapabilities(...)` literal (which ends at the line `rest_rate_limit_per_min=200,\n)`), add:

```python
# The real-money descriptor differs only in paper_only (ADR 0059 D9). Every
# other fact — IEX feed, stream caps, rate limit, buildable order types — is
# the same account tier; keeping one literal per mode makes the difference
# reviewable instead of a boolean flip buried in a constructor.
ALPACA_LIVE_CAPABILITIES = ALPACA_PAPER_CAPABILITIES.model_copy(update={"paper_only": False})
```

And:

```python
    def capabilities(self) -> BrokerCapabilities:
        return (
            ALPACA_LIVE_CAPABILITIES
            if get_alpaca_settings().is_live
            else ALPACA_PAPER_CAPABILITIES
        )
```

- [ ] **Step 4: Run the tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/test_capabilities.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/broker/alpaca/broker.py PythonDataService/tests/broker/alpaca/test_capabilities.py
git commit -m "feat(alpaca): capabilities select by mode; ALPACA_LIVE_CAPABILITIES joins the paper set (ADR 0059 D9)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Clerk selection names a mode disagreement (ADR 0059 D1)

**Files:**
- Modify: `PythonDataService/app/broker/alpaca/clerk/active_authority.py:206-225`
- Test: `PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py` (extend)

**Interfaces:**
- Consumes: `BrokerAccountModeDisagreement` (Task 4).
- Produces: `select_active_clerk_runtime` returns `_unavailable("LIVE_MODE_DISAGREEMENT", account_id=None, recovery=<error detail>)` when the account read raises the disagreement; the generic `BROKER_ACCOUNT_UNAVAILABLE` path is unchanged for every other exception. `LIVE_ACCOUNT_REFUSED` at line 220 is **untouched**.

- [ ] **Step 1: Write the failing test**

Append to `test_active_authority.py` (it already imports `select_active_clerk_runtime` and `Path`; no existing test there exercises a failing `get_account`, so this fake port is deliberately self-contained — `trade` is only bound, never called, before the refusal):

```python
async def test_mode_disagreement_is_named_not_folded_into_unavailable(tmp_path: Path) -> None:
    from app.broker.contract.errors import BrokerAccountModeDisagreement

    class _Read:
        broker_id = "alpaca"

        async def get_account(self):
            raise BrokerAccountModeDisagreement(
                "The configured Alpaca mode and the observed account disagree.",
                broker="alpaca",
                detail="ALPACA_MODE='live' but the account number begins with 'PA'",
            )

    runtime = await select_active_clerk_runtime(
        read=_Read(), trade=_Read(), artifacts_root=tmp_path,
    )

    assert runtime.authority_kind == "unavailable"
    assert runtime.startup_failure is not None
    assert runtime.startup_failure.reason_code == "LIVE_MODE_DISAGREEMENT"
    assert "begins with 'PA'" in runtime.startup_failure.recovery
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_active_authority.py -v -k disagreement`
Expected: FAIL — `reason_code == "BROKER_ACCOUNT_UNAVAILABLE"`.

- [ ] **Step 3: Implement**

In `select_active_clerk_runtime`, replace the first `try/except` (lines 206-219) with:

```python
    try:
        account = await read.get_account()
    except BrokerAccountModeDisagreement as exc:
        logger.warning(
            "Alpaca configured mode and observed account disagree; Clerk unavailable",
            extra={"action": "active_clerk_mode_disagreement", "detail": exc.detail},
        )
        return _unavailable(
            exc.reason_code,
            account_id=None,
            recovery=exc.detail or exc.message,
        )
    except Exception as exc:
        logger.warning(
            "Alpaca account identity could not be resolved; Clerk unavailable",
            extra={"action": "active_clerk_account_resolution_failed"},
            exc_info=True,
        )
        return _unavailable(
            "BROKER_ACCOUNT_UNAVAILABLE",
            account_id=None,
            recovery=f"Restore the Alpaca account identity probe: {exc}",
        )
```

Add the import `from app.broker.contract.errors import BrokerAccountModeDisagreement`.

- [ ] **Step 4: Run the active-authority tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/broker/alpaca/clerk/test_active_authority.py -v`
Expected: PASS, including the existing `LIVE_ACCOUNT_REFUSED` assertion.

- [ ] **Step 5: Commit**

```bash
git add PythonDataService/app/broker/alpaca/clerk/active_authority.py PythonDataService/tests/broker/alpaca/clerk/test_active_authority.py
git commit -m "feat(alpaca): clerk selection names LIVE_MODE_DISAGREEMENT instead of folding it into unavailable (ADR 0059 D1)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: The live verdict — schema, pure composer, endpoint (ADR 0059 D8)

Follows the repo's `add-fastapi-endpoint` skill: schema in `app/schemas/`, logic in `app/services/`, transport-only route.

**Files:**
- Create: `PythonDataService/app/schemas/alpaca_live_verdict.py`
- Create: `PythonDataService/app/services/alpaca_live_verdict.py`
- Modify: `PythonDataService/app/routers/brokers.py` (import + one route after `get_clerk_status`)
- Test: `PythonDataService/tests/services/test_alpaca_live_verdict.py` (create)
- Test: `PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py` (create)

**Interfaces:**
- Consumes: `AlpacaSettings` (Task 3), `ActiveClerkRuntime` / `ClerkStartupFailure` (`active_authority.py:95-160`), `get_active_clerk_runtime()`.
- Produces: `AlpacaLiveVerdict` (fields below); `alpaca_live_verdict(*, settings: AlpacaSettings | None, runtime: ActiveClerkRuntime | None, now_ms: int) -> AlpacaLiveVerdict`; `GET /api/brokers/{broker}/live-verdict` → `AlpacaLiveVerdict` (404 for any broker but `alpaca`).

- [ ] **Step 1: Write the failing service tests**

```python
# PythonDataService/tests/services/test_alpaca_live_verdict.py
"""The Alpaca live verdict is a pure function of settings and clerk selection
(ADR 0059 D8). It never contacts the broker and never guesses."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
)
from app.broker.alpaca.config import AlpacaSettings
from app.services.alpaca_live_verdict import alpaca_live_verdict

_NOW = 1_800_000_000_000
_LIVE = {
    "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
}


def _paper() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")


def _live() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **_LIVE)


def _failure(reason_code: str, account_id: str | None) -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="unavailable",
        account_id=account_id,
        startup_failure=ClerkStartupFailure(
            reason_code=reason_code,
            account_id=account_id,
            scope="ACCOUNT_CLERK",
            impact="x",
            recovery="y",
            observed_at_ms=_NOW,
        ),
    )


def test_unconfigured_settings_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=None, runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "unconfigured"
    assert verdict.final_verdict == "unknown"
    assert verdict.observed_at_ms == _NOW


def test_paper_settings_is_paper_regardless_of_clerk_state() -> None:
    verdict = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "paper"
    assert verdict.final_verdict == "paper"
    assert verdict.envelope_state == "not_applicable"
    assert verdict.armed_instance_count == 0


def test_live_with_refused_clerk_is_live_unarmed_and_names_the_account() -> None:
    runtime = _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "live"
    assert verdict.observed_account_id == "9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.clerk_refusal_reason_code == "LIVE_ACCOUNT_REFUSED"
    assert verdict.envelope_state == "configured_unsealed"
    assert verdict.final_verdict == "live-unarmed"
    assert "9LIVE0001" in verdict.headline


def test_live_with_mode_disagreement_is_unknown_and_disagreed() -> None:
    runtime = _failure("LIVE_MODE_DISAGREEMENT", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "disagreed"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_account_observed_is_unknown_and_unobserved() -> None:
    runtime = _failure("BROKER_ACCOUNT_UNAVAILABLE", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "unobserved"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_runtime_yet_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=_live(), runtime=None, now_ms=_NOW)

    assert verdict.clerk_authority == "not_installed"
    assert verdict.final_verdict == "unknown"


@pytest.mark.parametrize("final", ["paper", "live-unarmed", "unknown"])
def test_every_verdict_carries_server_authored_copy(final: str) -> None:
    settings, runtime = {
        "paper": (_paper(), None),
        "live-unarmed": (_live(), _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")),
        "unknown": (None, None),
    }[final]

    verdict = alpaca_live_verdict(settings=settings, runtime=runtime, now_ms=_NOW)

    assert verdict.final_verdict == final
    assert verdict.headline and verdict.detail
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.alpaca_live_verdict`.

- [ ] **Step 3: Implement the schema and the composer**

```python
# PythonDataService/app/schemas/alpaca_live_verdict.py
"""The server-derived Alpaca live verdict (ADR 0059 D8).

Extends ADR 0011's verdict principles to the Alpaca path: computed in the
data plane from settings and the clerk selection outcome, reactive on every
read, never composed by the Frontend, never a guess. Slice 1 renders the
verdict; arming, shadow and the envelope (slices 4-6) fill the fields that
this slice fixes at their empty values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConfiguredMode = Literal["paper", "live", "unconfigured"]
ModeAgreement = Literal["agreed", "disagreed", "unobserved"]
ClerkAuthority = Literal["sqlite", "synthetic", "unavailable", "not_installed"]
EnvelopeState = Literal["not_applicable", "configured_unsealed", "sealed"]
ShadowState = Literal["not_applicable", "none", "in_progress", "complete"]
FinalVerdict = Literal["paper", "live-unarmed", "live-armed", "unknown"]


class AlpacaLiveVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    configured_mode: ConfiguredMode
    observed_account_id: str | None
    mode_agreement: ModeAgreement
    clerk_authority: ClerkAuthority
    clerk_refusal_reason_code: str | None
    armed_instance_count: int = Field(ge=0)
    envelope_state: EnvelopeState
    shadow_state: ShadowState
    final_verdict: FinalVerdict
    # Operator copy is authored here, not in the client (CLAUDE.md hard rule).
    headline: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    observed_at_ms: int = Field(ge=0)
```

```python
# PythonDataService/app/services/alpaca_live_verdict.py
"""Compose the Alpaca live verdict from settings and clerk selection (ADR 0059 D8).

Pure: no broker I/O, no clock of its own. The router supplies ``now_ms``.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import (
    AlpacaLiveVerdict,
    ClerkAuthority,
    ModeAgreement,
)

_DISAGREEMENT = "LIVE_MODE_DISAGREEMENT"
_UNOBSERVED_REASONS = frozenset({"BROKER_ACCOUNT_UNAVAILABLE"})


def _clerk_authority(runtime: ActiveClerkRuntime | None) -> ClerkAuthority:
    if runtime is None:
        return "not_installed"
    return runtime.authority_kind


def _mode_agreement(
    *,
    account_id: str | None,
    refusal: str | None,
) -> ModeAgreement:
    if refusal == _DISAGREEMENT:
        return "disagreed"
    if account_id is None or refusal in _UNOBSERVED_REASONS:
        return "unobserved"
    return "agreed"


def alpaca_live_verdict(
    *,
    settings: AlpacaSettings | None,
    runtime: ActiveClerkRuntime | None,
    now_ms: int,
) -> AlpacaLiveVerdict:
    """Return the verdict for the current process state."""
    if settings is None:
        return AlpacaLiveVerdict(
            configured_mode="unconfigured",
            observed_account_id=None,
            mode_agreement="unobserved",
            clerk_authority=_clerk_authority(runtime),
            clerk_refusal_reason_code=None,
            armed_instance_count=0,
            envelope_state="not_applicable",
            shadow_state="not_applicable",
            final_verdict="unknown",
            headline="Alpaca is not configured",
            detail="No valid Alpaca settings are loaded, so the account mode cannot be stated.",
            observed_at_ms=now_ms,
        )

    failure = runtime.startup_failure if runtime is not None else None
    refusal = failure.reason_code if failure is not None else None
    account_id = runtime.selected_account_id if runtime is not None else None
    if account_id is None and failure is not None:
        account_id = failure.account_id
    authority = _clerk_authority(runtime)

    if settings.is_paper:
        return AlpacaLiveVerdict(
            configured_mode="paper",
            observed_account_id=account_id,
            mode_agreement=_mode_agreement(account_id=account_id, refusal=refusal),
            clerk_authority=authority,
            clerk_refusal_reason_code=refusal,
            armed_instance_count=0,
            envelope_state="not_applicable",
            shadow_state="not_applicable",
            final_verdict="paper",
            headline=f"Paper account{f' {account_id}' if account_id else ''} — no real money at risk",
            detail="ALPACA_MODE=paper. Orders reach Alpaca's paper endpoint only.",
            observed_at_ms=now_ms,
        )

    agreement = _mode_agreement(account_id=account_id, refusal=refusal)
    if agreement != "agreed" or runtime is None:
        why = {
            "disagreed": "the configured mode and the observed account disagree",
            "unobserved": "the account has not been observed yet",
        }[agreement if agreement != "agreed" else "unobserved"]
        return AlpacaLiveVerdict(
            configured_mode="live",
            observed_account_id=account_id,
            mode_agreement=agreement,
            clerk_authority=authority,
            clerk_refusal_reason_code=refusal,
            armed_instance_count=0,
            envelope_state="configured_unsealed",
            shadow_state="none",
            final_verdict="unknown",
            headline="Live mode configured — account state unknown",
            detail=f"ALPACA_MODE=live, but {why}. No live order can be admitted while the verdict is unknown.",
            observed_at_ms=now_ms,
        )

    # Slice 1: no arming exists, so an agreed live account is always unarmed.
    return AlpacaLiveVerdict(
        configured_mode="live",
        observed_account_id=account_id,
        mode_agreement="agreed",
        clerk_authority=authority,
        clerk_refusal_reason_code=refusal,
        armed_instance_count=0,
        envelope_state="configured_unsealed",
        shadow_state="none",
        final_verdict="live-unarmed",
        headline=f"LIVE account {account_id} — real money, no instance armed",
        detail=(
            "This is a real-money Alpaca account. No sealed instance is armed, so "
            "every order path refuses. Arming requires a completed shadow receipt "
            "and the supervised ceremony (ADR 0059)."
        ),
        observed_at_ms=now_ms,
    )
```

- [ ] **Step 4: Run the service tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/services/test_alpaca_live_verdict.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing endpoint test**

```python
# PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py
"""``GET /api/brokers/alpaca/live-verdict`` is transport over the pure composer."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
    set_active_clerk_runtime,
)
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER


def _headers() -> dict[str, str]:
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_alpaca_settings_for_testing()
    set_active_clerk_runtime(None)
    yield
    set_active_clerk_runtime(None)
    reset_alpaca_settings_for_testing()


async def test_paper_settings_serve_a_paper_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_MODE", "paper")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["configured_mode"] == "paper"
    assert body["final_verdict"] == "paper"
    assert body["headline"]


async def test_live_settings_with_refused_clerk_serve_live_unarmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s", "ALPACA_MODE": "live",
        "ALPACA_LIVE_LOSS_FRACTION": "0.02", "ALPACA_LIVE_LOSS_USD": "500",
        "ALPACA_LIVE_SHADOW_SESSIONS": "5", "ALPACA_LIVE_ARMING_MAX_SESSIONS": "20",
        "ALPACA_LIVE_XH_ENTRY_BPS": "10", "ALPACA_LIVE_XH_EXIT_BPS": "10",
    }.items():
        monkeypatch.setenv(name, value)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="unavailable",
            account_id="9LIVE0001",
            startup_failure=ClerkStartupFailure(
                reason_code="LIVE_ACCOUNT_REFUSED", account_id="9LIVE0001",
                scope="ACCOUNT_CLERK", impact="x", recovery="y", observed_at_ms=1,
            ),
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["final_verdict"] == "live-unarmed"
    assert body["observed_account_id"] == "9LIVE0001"
    assert body["armed_instance_count"] == 0


async def test_invalid_settings_serve_unknown_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_MODE", "live")  # no ALPACA_LIVE_* → invalid

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/live-verdict", headers=_headers())

    assert response.status_code == 200
    assert response.json()["configured_mode"] == "unconfigured"


async def test_other_brokers_are_404() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/ibkr/live-verdict", headers=_headers())

    assert response.status_code == 404
```

- [ ] **Step 6: Run to verify it fails**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/routers/test_alpaca_live_verdict_endpoint.py -v`
Expected: FAIL — 404 on `/api/brokers/alpaca/live-verdict`.

- [ ] **Step 7: Add the route**

In `routers/brokers.py`, add imports (alphabetically among the existing `app.` imports):

```python
from app.broker.alpaca.config import get_alpaca_settings
from app.schemas.alpaca_live_verdict import AlpacaLiveVerdict
from app.services.alpaca_live_verdict import alpaca_live_verdict
```

and, immediately after the `get_clerk_status` route (the function ending with `return sqlite_clerk_status(...)` around line 638-644), add:

```python
@router.get("/{broker}/live-verdict", response_model=AlpacaLiveVerdict)
async def get_live_verdict(broker: str) -> AlpacaLiveVerdict:
    """The server-derived live verdict (ADR 0059 D8). Pure; never contacts the broker."""
    if broker != "alpaca":
        raise HTTPException(
            status_code=404,
            detail={"reason": "live_verdict_unsupported_broker", "message": f"No live verdict for broker '{broker}'."},
        )
    from pydantic import ValidationError

    try:
        alpaca_settings = get_alpaca_settings()
    except ValidationError:
        # Invalid settings are a verdict input ("unconfigured"), not a 500.
        alpaca_settings = None
    return alpaca_live_verdict(
        settings=alpaca_settings,
        runtime=get_active_clerk_runtime(),
        now_ms=now_ms_utc(),
    )
```

(`get_active_clerk_runtime` and `now_ms_utc` are already imported at lines 21 and 92.)

- [ ] **Step 8: Run the endpoint tests**

Run: `cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest tests/routers/test_alpaca_live_verdict_endpoint.py tests/services/test_alpaca_live_verdict.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add PythonDataService/app/schemas/alpaca_live_verdict.py PythonDataService/app/services/alpaca_live_verdict.py PythonDataService/app/routers/brokers.py PythonDataService/tests/services/test_alpaca_live_verdict.py PythonDataService/tests/routers/test_alpaca_live_verdict_endpoint.py
git commit -m "feat(alpaca): server-derived live verdict at GET /api/brokers/alpaca/live-verdict (ADR 0059 D8)" -m "Pure composer over settings and clerk selection; never contacts the broker; copy authored server-side." -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Regenerate the contracts

**Files:**
- Generated: `contracts/openapi/python-data-service.openapi.json`, `Frontend/src/app/api/broker.types.ts`
- Modify: `Frontend/src/app/api/alpaca.types.ts` (one alias line)

- [ ] **Step 1: Export the OpenAPI contract**

Run: `cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py && .venv/bin/python scripts/export_openapi_contract.py --check`
Expected: the second command exits 0.

- [ ] **Step 2: Regenerate the Frontend types**

Run: `cd Frontend && npm run codegen:openapi && git diff --stat -- src/app/api/broker.types.ts`
Expected: the diff shows `AlpacaLiveVerdict` added and seven optional nullable fields on `BrokerAccountSnapshot` (`multiplier?: number | null`, …).

- [ ] **Step 3: Add the alias**

In `Frontend/src/app/api/alpaca.types.ts`, after the `BrokerAccountSnapshot` line add:

```ts
export type AlpacaLiveVerdict = components['schemas']['AlpacaLiveVerdict'];
```

- [ ] **Step 4: Verify the existing account-strip spec still type-checks (optional fields)**

Run: `podman exec my-frontend npx ng test --include='src/app/components/broker/v2-panel/account-strip/account-strip.component.spec.ts'`
Expected: PASS — the fixture literal omits the new optional fields and still satisfies the type.

- [ ] **Step 5: Commit**

```bash
git add contracts/openapi/python-data-service.openapi.json Frontend/src/app/api/broker.types.ts Frontend/src/app/api/alpaca.types.ts
git commit -m "chore(contracts): regenerate OpenAPI and Frontend types for the live verdict and margin fields (ADR 0059 slice 1)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Frontend — verdict service and the global Alpaca banner (ADR 0059 D8)

Follows the repo's `build-angular-component` skill and `.claude/rules/angular.md`.

**Files:**
- Modify: `Frontend/src/app/services/brokers.service.ts` (one method after `getAccount`)
- Create: `Frontend/src/app/services/alpaca-live-verdict.service.ts` + `alpaca-live-verdict.service.spec.ts`
- Create: `Frontend/src/app/shell/alpaca-live-banner.component.ts` + `alpaca-live-banner.component.spec.ts`
- Modify: `Frontend/src/app/app.component.ts:5-28,72-74,101-104`

**Interfaces:**
- Consumes: `AlpacaLiveVerdict` (Task 8).
- Produces: `BrokersService.getLiveVerdict(): Promise<AlpacaLiveVerdict>`; `AlpacaLiveVerdictService` (root) with `verdict: WritableSignal<AlpacaLiveVerdict | null>`, `lastError`, `start()`, `refresh()`; `<app-alpaca-live-banner shell-connection />`.

- [ ] **Step 1: Write the failing service spec**

```ts
// Frontend/src/app/services/alpaca-live-verdict.service.spec.ts
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { AlpacaLiveVerdictService } from './alpaca-live-verdict.service';
import { BrokersService } from './brokers.service';

class FakeBrokersService {
  getLiveVerdict = vi.fn();
}

function makeVerdict(overrides: Partial<AlpacaLiveVerdict> = {}): AlpacaLiveVerdict {
  return {
    configured_mode: 'paper',
    observed_account_id: 'PA9',
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    shadow_state: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function setup() {
  const brokers = new FakeBrokersService();
  TestBed.configureTestingModule({ providers: [{ provide: BrokersService, useValue: brokers }] });
  return { svc: TestBed.inject(AlpacaLiveVerdictService), brokers };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('AlpacaLiveVerdictService', () => {
  it('holds null before the first response', () => {
    const { svc } = setup();
    expect(svc.verdict()).toBeNull();
  });

  it('refresh() stores the server verdict verbatim', async () => {
    const { svc, brokers } = setup();
    const v = makeVerdict({ final_verdict: 'live-unarmed', configured_mode: 'live' });
    brokers.getLiveVerdict.mockResolvedValue(v);

    await svc.refresh();

    expect(svc.verdict()).toEqual(v);
    expect(svc.lastError()).toBeNull();
  });

  it('refresh() clears the verdict and records the error when the read fails', async () => {
    const { svc, brokers } = setup();
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());
    await svc.refresh();
    brokers.getLiveVerdict.mockRejectedValue(new Error('down'));

    await svc.refresh();

    expect(svc.verdict()).toBeNull();
    expect(svc.lastError()).toBeInstanceOf(Error);
  });

  it('start() is idempotent and refreshes immediately', async () => {
    const { svc, brokers } = setup();
    brokers.getLiveVerdict.mockResolvedValue(makeVerdict());

    svc.start();
    svc.start();
    await Promise.resolve();

    expect(brokers.getLiveVerdict).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `podman exec my-frontend npx ng test --include='src/app/services/alpaca-live-verdict.service.spec.ts'`
Expected: FAIL — cannot resolve `./alpaca-live-verdict.service`.

- [ ] **Step 3: Implement the fetch and the service**

In `brokers.service.ts`, add the type import `AlpacaLiveVerdict` to the `'../api/alpaca.types'` import list, and after `getAccount(...)` add:

```ts
  /**
   * The server-derived Alpaca live verdict (ADR 0059 D8). Pure on the
   * server — it never contacts the broker — so it is safe to poll from the
   * shell. The client renders it and never composes one (ADR 0011 §7).
   */
  getLiveVerdict(): Promise<AlpacaLiveVerdict> {
    return firstValueFrom(this.http.get<AlpacaLiveVerdict>(`${this.base}/alpaca/live-verdict`));
  }
```

Create the service:

```ts
// Frontend/src/app/services/alpaca-live-verdict.service.ts
import { DestroyRef, Injectable, inject, signal } from '@angular/core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { BrokersService } from './brokers.service';

const POLL_INTERVAL_MS = 5000;

/**
 * Singleton owner of the Alpaca live verdict signal (ADR 0059 D8).
 *
 * Polls ``GET /api/brokers/alpaca/live-verdict`` every five seconds and
 * exposes the latest verdict as a signal. The shell renders the global
 * Alpaca account-mode banner from it. Mirrors ``BrokerHealthService`` for
 * IBKR; the two are separate because they answer separate questions
 * (market-data connection vs. real-money account mode).
 *
 * Never derive the mode from an env var or an account-id shape here: the
 * server's verdict is the only source of truth (ADR 0011 §7).
 */
@Injectable({ providedIn: 'root' })
export class AlpacaLiveVerdictService {
  private readonly brokers = inject(BrokersService);
  private readonly destroyRef = inject(DestroyRef);

  readonly verdict = signal<AlpacaLiveVerdict | null>(null);
  readonly lastError = signal<unknown | null>(null);

  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  /** Begin background polling. Idempotent — the shell calls this once on boot. */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.refresh();
    this.pollTimer = setInterval(() => void this.refresh(), POLL_INTERVAL_MS);
  }

  async refresh(): Promise<void> {
    try {
      this.verdict.set(await this.brokers.getLiveVerdict());
      this.lastError.set(null);
    } catch (err) {
      // A failed read means the verdict is unknown to this client; never
      // keep rendering a stale "paper" over a network fault.
      this.lastError.set(err);
      this.verdict.set(null);
    }
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}
```

- [ ] **Step 4: Run the service spec**

Run: `podman exec my-frontend npx ng test --include='src/app/services/alpaca-live-verdict.service.spec.ts'`
Expected: PASS.

- [ ] **Step 5: Write the failing banner spec**

```ts
// Frontend/src/app/shell/alpaca-live-banner.component.spec.ts
import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { AlpacaLiveBannerComponent } from './alpaca-live-banner.component';

function verdict(overrides: Partial<AlpacaLiveVerdict>): AlpacaLiveVerdict {
  return {
    configured_mode: 'paper',
    observed_account_id: 'PA9',
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    shadow_state: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

async function renderWith(v: AlpacaLiveVerdict | null) {
  return render(AlpacaLiveBannerComponent, {
    providers: [{ provide: AlpacaLiveVerdictService, useValue: { verdict: signal(v), lastError: signal(null) } }],
  });
}

describe('AlpacaLiveBannerComponent', () => {
  it('renders nothing before the first verdict', async () => {
    await renderWith(null);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders the server headline for a paper account, quietly', async () => {
    await renderWith(verdict({}));
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Paper account PA9');
    expect(status.className).toContain('is-paper');
  });

  it('renders a live-unarmed account loudly with the account id and armed count', async () => {
    await renderWith(
      verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
      }),
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-unarmed');
    expect(status.textContent).toContain('9LIVE0001');
    expect(status.textContent).toContain('0 armed');
    expect(status.getAttribute('aria-label')).toContain('real money');
  });

  it('renders unknown as a warning that names the disagreement code', async () => {
    await renderWith(
      verdict({
        configured_mode: 'live',
        observed_account_id: null,
        mode_agreement: 'disagreed',
        clerk_refusal_reason_code: 'LIVE_MODE_DISAGREEMENT',
        final_verdict: 'unknown',
        headline: 'Live mode configured — account state unknown',
        detail: 'the configured mode and the observed account disagree',
      }),
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-unknown');
    expect(status.textContent).toContain('Live Mode Disagreement');
  });
});
```

- [ ] **Step 6: Run to verify it fails**

Run: `podman exec my-frontend npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'`
Expected: FAIL — cannot resolve `./alpaca-live-banner.component`.

- [ ] **Step 7: Implement the banner**

```ts
// Frontend/src/app/shell/alpaca-live-banner.component.ts
import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';

/**
 * Global Alpaca account-mode banner (ADR 0059 D8; the ADR 0011 trust anchor
 * for the Alpaca path). Renders the server verdict and nothing it derives
 * itself: the headline and detail are backend-authored, the mode class is
 * the verdict's own ``final_verdict``. On a live account the account id,
 * the mode and the armed count are always on screen.
 */
@Component({
  selector: 'app-alpaca-live-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  styles: [`
    :host { display: contents; }
    .alpaca-banner {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.25rem 0.75rem; border-radius: var(--radius-pill);
      font-size: 0.8rem; line-height: 1.2; white-space: nowrap;
      border: 1px solid var(--border-strong); color: var(--text-secondary);
    }
    .alpaca-banner.is-paper { background: var(--info-soft); color: var(--info); border-color: rgba(41, 182, 246, 0.42); }
    .alpaca-banner.is-live-unarmed { background: var(--warn-soft, #fff3e0); color: var(--warn, #b45309); border-color: currentColor; font-weight: 600; }
    .alpaca-banner.is-live-armed { background: var(--danger-soft, #fde8e8); color: var(--danger, #b91c1c); border-color: currentColor; font-weight: 700; }
    .alpaca-banner.is-unknown { background: var(--surface-sunken, transparent); color: var(--text-secondary); border-style: dashed; }
    .alpaca-banner__kicker { font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; font-size: 0.68rem; }
    .alpaca-banner__armed { opacity: 0.85; }
  `],
  template: `
    @let v = verdict();
    @if (v) {
      <div
        class="alpaca-banner"
        [class.is-paper]="v.final_verdict === 'paper'"
        [class.is-live-unarmed]="v.final_verdict === 'live-unarmed'"
        [class.is-live-armed]="v.final_verdict === 'live-armed'"
        [class.is-unknown]="v.final_verdict === 'unknown'"
        role="status"
        [attr.aria-label]="ariaLabel()"
        [attr.title]="v.detail"
      >
        <span class="alpaca-banner__kicker">Alpaca</span>
        <span>{{ v.headline }}</span>
        @if (v.configured_mode === 'live') {
          <span class="alpaca-banner__armed">· {{ v.armed_instance_count }} armed</span>
        }
        @if (v.clerk_refusal_reason_code && v.final_verdict === 'unknown') {
          <span>· {{ v.clerk_refusal_reason_code | receiptLabel }}</span>
        }
      </div>
    }
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);

  protected readonly verdict = this.service.verdict;

  protected readonly ariaLabel = computed(() => {
    const v = this.verdict();
    if (!v) return '';
    const money = v.configured_mode === 'live' ? 'real money' : 'no real money at risk';
    return `Alpaca account mode: ${v.final_verdict}, ${money}. ${v.headline}`;
  });
}
```

- [ ] **Step 8: Mount it in the shell and start the poll**

In `app.component.ts`: add `import { AlpacaLiveBannerComponent } from './shell/alpaca-live-banner.component';` and `import { AlpacaLiveVerdictService } from './services/alpaca-live-verdict.service';`; add `AlpacaLiveBannerComponent` to `imports`; change the template line `<app-broker-banner shell-connection />` to:

```html
        <app-broker-banner shell-connection />
        <app-alpaca-live-banner shell-connection />
```

(`TopBarComponent` projects every `[shell-connection]` element into its slot.) Add `private readonly alpacaLive = inject(AlpacaLiveVerdictService);` beside `brokerHealth`, and in the constructor after `this.brokerHealth.start();` add:

```ts
    // The Alpaca account-mode banner is the ADR 0011 trust anchor for the
    // Alpaca path (ADR 0059 D8): one root poll, rendered from the server
    // verdict, never composed on the client.
    this.alpacaLive.start();
```

- [ ] **Step 9: Run the banner spec and the app spec**

Run: `podman exec my-frontend npx ng test --include='src/app/shell/alpaca-live-banner.component.spec.ts'` then `podman exec my-frontend npx ng test --include='src/app/app.component.spec.ts'`. That spec provides `FakeBrokerHealthService` through `useClass` (lines 11 and 43); add beside it

```ts
class FakeAlpacaLiveVerdictService {
  verdict = signal(null);
  lastError = signal(null);
  start = vi.fn();
}
```

and `{ provide: AlpacaLiveVerdictService, useClass: FakeAlpacaLiveVerdictService }` in the same `providers` array (import `AlpacaLiveVerdictService` from `./services/alpaca-live-verdict.service`, and `signal` / `vi` if not already imported).
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add Frontend/src/app/services/brokers.service.ts Frontend/src/app/services/alpaca-live-verdict.service.ts Frontend/src/app/services/alpaca-live-verdict.service.spec.ts Frontend/src/app/shell/alpaca-live-banner.component.ts Frontend/src/app/shell/alpaca-live-banner.component.spec.ts Frontend/src/app/app.component.ts Frontend/src/app/app.component.spec.ts
git commit -m "feat(shell): global Alpaca account-mode banner rendered from the server live verdict (ADR 0059 D8)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Frontend — the two hardcoded "Paper" chrome sites become mode-driven; the card shows margin (ADR 0059 D7, D8; ADR 0011)

**Files:**
- Modify: `Frontend/src/app/components/broker/v2-panel/account-strip/account-strip.component.html:8,19`
- Modify: `Frontend/src/app/components/broker/v2-panel/account-strip/account-strip.component.scss:70-74`
- Modify: `Frontend/src/app/components/broker/v2-panel/account-strip/account-strip.component.spec.ts` (one test)
- Modify: `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.html:14,20-37`
- Modify: `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts` (two tests)

- [ ] **Step 1: Write the failing strip test**

Append to `account-strip.component.spec.ts` (reuse its existing `account` fixture and render helper — read the file's existing `render(AccountStripComponent, { inputs: ... })` call and mirror it):

```ts
it('renders a live account with live chrome, never paper chrome', async () => {
  await render(AccountStripComponent, {
    inputs: { account: { ...account, account_id: '9LIVE0001', account_mode: 'live' }, clerkStatus: null },
    providers: [provideRouter([])],
  });

  const badge = screen.getByText('Live');
  expect(badge.className).toContain('posture-badge--live');
  expect(badge.className).not.toContain('posture-badge--paper');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `podman exec my-frontend npx ng test --include='src/app/components/broker/v2-panel/account-strip/account-strip.component.spec.ts'`
Expected: FAIL — class contains `posture-badge--paper`.

- [ ] **Step 3: Fix the strip**

`account-strip.component.html` line 19 becomes:

```html
      <span
        class="posture-badge"
        [class.posture-badge--paper]="snap.account_mode === 'paper'"
        [class.posture-badge--live]="snap.account_mode === 'live'"
      >{{ snap.account_mode | receiptLabel }}</span>
```

and line 8 `<span>Resolving paper account</span>` becomes `<span>Resolving account</span>`.

`account-strip.component.scss` — after the `.posture-badge--paper` block add:

```scss
.posture-badge--live {
  border-color: rgba(220, 38, 38, 0.55);
  background: var(--danger-soft, #fde8e8);
  color: var(--danger, #b91c1c);
  font-weight: 600;
}
```

- [ ] **Step 4: Run the strip spec**

Run: `podman exec my-frontend npx ng test --include='src/app/components/broker/v2-panel/account-strip/account-strip.component.spec.ts'`
Expected: PASS.

- [ ] **Step 5: Write the failing card tests**

Append to `alpaca-account-card.component.spec.ts`:

The file already defines `fakeAccount(overrides)` and `renderCard(getAccount)`; use them as they are:

```ts
it('tags a live account as Live with danger severity, never a hardcoded Paper', async () => {
  await renderCard(() => Promise.resolve(fakeAccount({ account_id: '9LIVE0001', account_mode: 'live' })));

  expect(await screen.findByText('9LIVE0001')).toBeTruthy();
  expect(screen.getByText('Live')).toBeTruthy();
  expect(screen.queryByText('Paper')).toBeNull();
});

it('renders the margin facts read-only, with a dash for an unknown value', async () => {
  await renderCard(() =>
    Promise.resolve(
      fakeAccount({
        multiplier: 4,
        regt_buying_power: 200_000,
        daytrading_buying_power: null,
        maintenance_margin: 0,
        initial_margin: 0,
        sma: 100_000,
        last_equity: 100_000,
      }),
    ),
  );

  expect(await screen.findByText('Multiplier')).toBeTruthy();
  expect(screen.getByText('Multiplier').nextElementSibling?.textContent).toContain('4');
  expect(screen.getByText('Day-trading BP').nextElementSibling?.textContent).toContain('—');
});
```

(The existing test `renders account figures and a paper badge when loaded` still passes: `'paper' | receiptLabel` renders `Paper`.)

- [ ] **Step 6: Run to verify they fail**

Run: `podman exec my-frontend npx ng test --include='src/app/components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts'`
Expected: FAIL — "Paper" is rendered; no "Multiplier" row.

- [ ] **Step 7: Fix the card**

`alpaca-account-card.component.html` line 14 `<p-tag severity="warn" value="Paper" />` becomes:

```html
          <p-tag [severity]="acct.account_mode === 'live' ? 'danger' : 'warn'" [value]="acct.account_mode | receiptLabel" />
```

After the `headline-metrics__freshness` `<div>` (before `</dl>` at line 37) insert:

```html
        <div>
          <dt>Multiplier</dt>
          <dd>{{ acct.multiplier ?? '—' }}</dd>
        </div>
        <div>
          <dt>Reg T BP</dt>
          <dd>{{ acct.regt_buying_power == null ? '—' : (acct.regt_buying_power | currency: acct.currency) }}</dd>
        </div>
        <div>
          <dt>Day-trading BP</dt>
          <dd>{{ acct.daytrading_buying_power == null ? '—' : (acct.daytrading_buying_power | currency: acct.currency) }}</dd>
        </div>
        <div>
          <dt>Maintenance margin</dt>
          <dd>{{ acct.maintenance_margin == null ? '—' : (acct.maintenance_margin | currency: acct.currency) }}</dd>
        </div>
        <div>
          <dt>Initial margin</dt>
          <dd>{{ acct.initial_margin == null ? '—' : (acct.initial_margin | currency: acct.currency) }}</dd>
        </div>
        <div>
          <dt>SMA</dt>
          <dd>{{ acct.sma == null ? '—' : (acct.sma | currency: acct.currency) }}</dd>
        </div>
        <div>
          <dt>Last equity</dt>
          <dd>{{ acct.last_equity == null ? '—' : (acct.last_equity | currency: acct.currency) }}</dd>
        </div>
```

(If the template passes ~80 lines after this, extract a `alpaca-account-margin.component.ts` presentation component taking `account = input.required<BrokerAccountSnapshot>()` and move the seven rows there; the rule is the `angular.md` template ceiling, and the extraction is the reviewer's call, not a reason to skip the rows.)

- [ ] **Step 8: Run the card spec**

Run: `podman exec my-frontend npx ng test --include='src/app/components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts'`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add Frontend/src/app/components/broker/v2-panel/account-strip/ Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.html Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.spec.ts
git commit -m "fix(alpaca-desk): account chrome follows the observed mode; the card renders margin facts read-only (ADR 0059 D7/D8, ADR 0011)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Lint, targeted suites, thermo, PR

- [ ] **Step 1: Project-scope lint**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/ && npx eslint Frontend/src/ --max-warnings 0`
Expected: both exit 0.

- [ ] **Step 2: Targeted Python suites — every surface touched plus every consumer of a changed helper**

Run:
```bash
cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest \
  tests/broker/alpaca tests/broker/v2panel tests/services/test_alpaca_live_verdict.py \
  tests/routers/test_alpaca_live_verdict_endpoint.py tests/routers/test_broker_bots.py \
  tests/routers/test_alpaca_clerk_sqlite.py tests/schemas -q
```
Expected: all pass; in particular the five `LIVE_ACCOUNT_REFUSED` assertions.

- [ ] **Step 3: Contract checks**

Run: `cd PythonDataService && .venv/bin/python scripts/export_openapi_contract.py --check && cd ../Frontend && npm run codegen:check`
Expected: both exit 0.

- [ ] **Step 4: Full Frontend suite in chunks** (a parent spec may pin a child's copy — see `feedback_scoped_frontend_runs_miss_ci`)

Run: `podman exec my-frontend npx ng test --include='src/app/shell/**/*.spec.ts'` then `--include='src/app/components/broker/**/*.spec.ts'` then `--include='src/app/components/brokers/**/*.spec.ts'` then `--include='src/app/services/**/*.spec.ts'`.
Expected: all pass; exit 137 means OOM — rerun that chunk smaller.

- [ ] **Step 5: Thermonuclear review, by a fresh reviewer, once**

Invoke the `thermo-nuclear-code-quality-review` skill against the branch diff (`git diff master...HEAD`). Fix every **major** finding in-branch; note minor ones in the PR body if deferred. Do not self-review.

- [ ] **Step 6: Open the PR**

Title: `feat(alpaca): ADR 0059 slice 1 — worlds and identity; a live account is visible and every order path still refuses`

Body (fill from the commits):

```
## What
ADR 0059 slice 1 (Consequences §1). Four closed account worlds; `ALPACA_MODE=live` admitted only with every `ALPACA_LIVE_*` value; the adapter derives `account_mode` from settings and refuses a disagreeing account-number shape; capabilities select by mode; seven margin fields ride the account snapshot; `GET /api/brokers/alpaca/live-verdict` and a global top-bar banner; the two hardcoded "Paper" chrome sites follow the observed mode.

## What does NOT change
None of the thirteen `!= "paper"` gates. `LIVE_ACCOUNT_REFUSED` still fires for a live account at clerk selection; deploy, manual orders, recovery and dev-reset still refuse. The five tests pinning that are green.

## Contracts
OpenAPI + Frontend types regenerated (`AlpacaLiveVerdict`; optional nullable margin fields on `BrokerAccountSnapshot`).

## Tests
<paste the targeted-suite summary lines>

## Thermo
<major findings fixed / minor deferred>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## Self-review

**Spec coverage (ADR 0059 → tasks):** D1 worlds → T1, T2; D1 mode agreement in settings → T3; D1 adapter derivation + shape refusal → T4; D1 `LIVE_MODE_DISAGREEMENT` at selection → T6; D4 env-sourced values required when live → T3; D7 margin fields ingested and rendered → T4, T10; D8 verdict + banner + no client composition → T7, T9; D9 capabilities by mode → T5; slice-1 outcome "visible, every gate refuses" → Global Constraints + T11 step 2. D10 (fault injection / dev-reset refuse live) needs no change in this slice: `fault_injection.py:87` gates on `is_paper`, which is unchanged, and `dev_reset.py:108` is one of the untouched gates.

**Placeholder scan:** every deferred fact was pinned on 2026-09-07 before dispatch — the fixture's margin literals (Task 4), `BrokerCapabilities` being a frozen Pydantic model (Task 5), the absence of any failing-`get_account` precedent in `test_active_authority.py` (Task 6), the card spec's `fakeAccount`/`renderCard` helpers (Task 10) and `app.component.spec.ts`'s `useClass` fake (Task 9). No step tells the executor to go and find something out.

**Type consistency:** `require_real_account_id` (T1) is the name used in T1's `runtime.py` edit and `shadow_account_id_for_live_account`. `BrokerAccountModeDisagreement.reason_code == "LIVE_MODE_DISAGREEMENT"` (T4) is the value T6 forwards and T7's composer matches on `_DISAGREEMENT`. `AlpacaSettings.is_live` (T3) is what T5 and T7 read. `ActiveClerkRuntime.selected_account_id` and `ClerkStartupFailure.account_id` (existing) are what T7 composes. `AlpacaLiveVerdict.final_verdict` literals (`paper | live-unarmed | live-armed | unknown`) are the class names T9's banner binds (`is-paper`, `is-live-unarmed`, `is-live-armed`, `is-unknown`).
