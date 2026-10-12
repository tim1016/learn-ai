"""What the lake wrote down when it last failed to fetch a thing a session needs (#3030).

Formula: none. A thing's row in the lake's catalog is ``failed`` with a last error and a count of the attempts spent.
  Whether the lake would ask for it again on the next capture is the lake's to say, not this file's:
  ``option_lake.failed_row_is_asked_again`` for a contract's day and a listing, ``option_lake.stock_day_is_asked_again``
  for the underlying's day. This file hands each the recipe the capture of that thing runs under.
Reference: ``app/data_lake/option_lake`` for the two functions; ``app/data_lake/catalog_client`` for the claim state.
Canonical implementation: this file, for the identities of the three kinds of row a session needs; the lake's capture
  functions are where they are made, and the tests pin that this file finds the rows they write.
Validated against: tests/options_backtest/test_lake_check.py.

Reading only. The catalog's claim states are asked on the process's background loop, the way a worker thread asks
the lake anything that is not a file (``materialize_engine_run`` crosses the same way); no claim is made and no
vendor is asked. That loop's catalog pool is opened here before the first read: nothing else opens it in a process
that has not yet downloaded anything, and a check made first would otherwise answer "catalog unavailable" for ever.
"""

from __future__ import annotations

from datetime import date

from app.data_lake import catalog_client
from app.data_lake.ensure_data import provider_for_data_type
from app.data_lake.option_bars import option_minute_trade_dch
from app.data_lake.option_lake import (
    OPTION_MARKET,
    OPTION_PRICE_ADJUSTMENT_MODE,
    OPTION_PROVIDER,
    failed_row_is_asked_again,
    stock_day_is_asked_again,
)
from app.data_lake.option_listing import option_listing_dch
from app.data_lake.types import ArtifactIdentity
from app.options.contract import OptionContract
from app.options_backtest.data_check import DataKind, DataThing, RecordedFailure
from app.options_backtest.lake_data import LakeRunData
from app.utils.background_loop import run_on_background_loop

# A claim-state read is a single indexed select.
_CATALOG_READ_TIMEOUT_SECONDS = 60.0


def _stock_day_identity(symbol: str, day: date) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol=symbol,
        trading_date=day,
        resolution="minute",
        data_type="trade",
        provider=provider_for_data_type("trade"),
        price_adjustment_mode="raw",
    )


def _option_day_identity(contract: OptionContract, day: date) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market=OPTION_MARKET,
        symbol=contract.occ_symbol,
        trading_date=day,
        resolution="minute",
        data_type="trade",
        provider=OPTION_PROVIDER,
        price_adjustment_mode=OPTION_PRICE_ADJUSTMENT_MODE,
    )


class LakeCheckData(LakeRunData):
    """``LakeRunData`` that can also say what the lake recorded when a fetch failed. For the data check."""

    def recorded_failure(self, thing: DataThing, trading_date: date) -> RecordedFailure | None:
        """The failure the lake recorded for ``thing`` on ``trading_date``, or ``None`` when it recorded none.

        Raises ``CatalogUnavailableError`` when the catalog cannot be asked, and ``RuntimeError`` on a thread with a
        running event loop.
        """
        state = run_on_background_loop(self._claim_state(thing, trading_date), timeout=_CATALOG_READ_TIMEOUT_SECONDS)
        if state is None or state.status != "failed":
            return None
        return RecordedFailure(
            reason=state.last_error or "unknown",
            will_be_asked_again=self._would_be_asked_again(thing, state, trading_date),
        )

    def _would_be_asked_again(
        self, thing: DataThing, state: catalog_client.ArtifactClaimState, trading_date: date
    ) -> bool:
        if thing.kind is DataKind.UNDERLYING_BARS:
            return stock_day_is_asked_again(state, trading_date)
        if thing.kind is DataKind.LISTING:
            listing_recipe = option_listing_dch(self._underlying, trading_date)
            return failed_row_is_asked_again(state, data_contract_hash=listing_recipe)
        return failed_row_is_asked_again(state, data_contract_hash=option_minute_trade_dch())

    async def _claim_state(self, thing: DataThing, trading_date: date) -> catalog_client.ArtifactClaimState | None:
        await catalog_client.init_pool()
        if thing.kind is DataKind.UNDERLYING_BARS:
            return await catalog_client.select_minute_bar_claim_state(
                _stock_day_identity(self._underlying, trading_date)
            )
        if thing.kind is DataKind.LISTING:
            return await catalog_client.select_metadata_claim_state(option_listing_dch(self._underlying, trading_date))
        assert thing.contract is not None  # a contracts thing always names its contract
        return await catalog_client.select_minute_bar_claim_state(_option_day_identity(thing.contract, trading_date))
