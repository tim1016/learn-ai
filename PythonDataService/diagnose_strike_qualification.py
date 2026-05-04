"""One-shot diagnostic: which strikes does IBKR's reqSecDefOptParams claim
exist for SPY at expiry X, vs which actually pass qualifyContractsAsync?

Run inside the container:
    podman exec polygon-data-service python /app/diagnose_strike_qualification.py
"""

from __future__ import annotations

import asyncio
import os

from ib_async import IB, Option, Stock


async def main() -> None:
    host = os.environ.get("IBKR_HOST", "172.23.176.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    ib = IB()
    await ib.connectAsync(host=host, port=port, clientId=99, readonly=True)
    try:
        stock = (await ib.qualifyContractsAsync(Stock("SPY", "SMART", "USD")))[0]
        params = await ib.reqSecDefOptParamsAsync(
            underlyingSymbol="SPY",
            futFopExchange="",
            underlyingSecType=stock.secType,
            underlyingConId=stock.conId,
        )

        all_expirations: set[str] = set()
        all_strikes: set[float] = set()
        for p in params:
            all_expirations.update(p.expirations)
            all_strikes.update(p.strikes)
        sorted_expirations = sorted(all_expirations)

        print(f"SPY conId={stock.conId}, host={host}:{port}")
        print(f"reqSecDefOptParams reports {len(sorted_expirations)} expirations")
        print(f"  first 5: {sorted_expirations[:5]}")
        print(f"reqSecDefOptParams reports {len(all_strikes)} unique strikes (union across expiries)")

        target = sorted_expirations[0]
        strikes_set: set[float] = set()
        for p in params:
            if target in p.expirations:
                strikes_set.update(float(k) for k in p.strikes)
        strikes_for_target = sorted(strikes_set)
        print(f"\nTarget expiry: {target}")
        print(f"  metadata claims {len(strikes_for_target)} unique strikes for this expiry")
        if strikes_for_target:
            print(f"  range: [{min(strikes_for_target)}, {max(strikes_for_target)}]")

        atm_band = [k for k in strikes_for_target if 540 <= k <= 600]
        print(f"\nProbing {len(atm_band)} strikes in [540, 600] band:")

        contracts = []
        for k in atm_band:
            for right in ("C", "P"):
                contracts.append(
                    Option(
                        symbol="SPY",
                        lastTradeDateOrContractMonth=target,
                        strike=k,
                        right=right,
                        exchange="SMART",
                        currency="USD",
                        multiplier="100",
                    )
                )
        qualified = await ib.qualifyContractsAsync(*contracts)
        qualified_keys: set[tuple[float, str]] = {
            (float(c.strike), c.right) for c in qualified if c is not None
        }
        none_count = sum(1 for c in qualified if c is None)

        print(f"  qualifyContractsAsync: input {len(contracts)}, returned-list len {len(qualified)}, "
              f"None entries: {none_count}")
        print()
        print(f"  {'strike':>8s}  {'C':>3s}  {'P':>3s}")
        print(f"  {'-' * 8}  {'-' * 3}  {'-' * 3}")
        for k in atm_band:
            c_ok = "OK" if (k, "C") in qualified_keys else "--"
            p_ok = "OK" if (k, "P") in qualified_keys else "--"
            print(f"  {k:>8.1f}  {c_ok:>3s}  {p_ok:>3s}")

        good = [k for k in atm_band if (k, "C") in qualified_keys and (k, "P") in qualified_keys]
        partial = [
            k for k in atm_band
            if ((k, "C") in qualified_keys) ^ ((k, "P") in qualified_keys)
        ]
        bad = [
            k for k in atm_band
            if (k, "C") not in qualified_keys and (k, "P") not in qualified_keys
        ]
        print(f"\nSummary: {len(good)} fully-qualified, {len(partial)} half-qualified, "
              f"{len(bad)} fully-rejected (out of {len(atm_band)} listed)")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
