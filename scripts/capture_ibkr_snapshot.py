"""Capture an IBKR option market data snapshot for OPT-IB-002.

Connects to IBKR TWS or IB Gateway, fetches SPY option chain data including
IBKR's model-computed implied volatility, and writes an Arrow IPC file that
the OPT-IB-002 fixture generator reads.

Usage
-----
    # Paper account (TWS default port 7497):
    python scripts/capture_ibkr_snapshot.py

    # Live account (TWS port 7496):
    python scripts/capture_ibkr_snapshot.py --port 7496

    # Different symbol or output directory:
    python scripts/capture_ibkr_snapshot.py --symbol SPY --output scripts/ibkr_snapshots/opt_ib_002/

Requirements
------------
    pip install ib_insync pyarrow

    IBKR TWS or IB Gateway must be running with API access enabled.
    For paper trading: File → Global Configuration → API → Enable Active X and Socket Clients.
    Socket port: 7497 (paper) or 7496 (live).

Output
------
    scripts/ibkr_snapshots/opt_ib_002/snapshot_YYYYMMDD_HHMMSS.arrow

Contract filter (applied to avoid degenerate IV comparisons)
------------------------------------------------------------
    - Strike within ±10% of spot
    - Expiry between 7 and 90 calendar days from now
    - Bid ≥ $0.05, ask > 0, ask ≥ bid (rejects one-sided and crossed quotes)
    - IBKR modelGreeks.optPrice > 0 (model price must be present)
    - IBKR modelGreeks.impliedVol between 0.05 and 2.0

Rates
-----
    --rate   Continuously compounded risk-free rate (default: 0.0525, ~Fed Funds 2026)
    --div    Dividend yield for the underlying (default: 0.013, ~SPY trailing yield)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="SPY", help="Underlying symbol (default: SPY)")
    p.add_argument("--host", default="127.0.0.1", help="TWS/Gateway host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=7497, help="TWS/Gateway port (default: 7497 paper)")
    p.add_argument("--client-id", type=int, default=10, help="IBKR client ID (default: 10)")
    p.add_argument(
        "--output",
        default="scripts/ibkr_snapshots/opt_ib_002/",
        help="Output directory for the arrow snapshot",
    )
    p.add_argument("--rate", type=float, default=0.0525, help="Risk-free rate (default: 0.0525)")
    p.add_argument("--div", type=float, default=0.013, help="Dividend yield (default: 0.013 for SPY)")
    p.add_argument(
        "--min-days",
        type=int,
        default=7,
        help="Minimum days to expiry to include (default: 7)",
    )
    p.add_argument(
        "--max-days",
        type=int,
        default=90,
        help="Maximum days to expiry to include (default: 90)",
    )
    p.add_argument(
        "--moneyness-pct",
        type=float,
        default=10.0,
        help="±%% of spot to include for strike filter (default: 10)",
    )
    return p.parse_args(argv)


def _expiry_close_ms(expiry_str: str) -> int:
    """Convert IBKR expiry string (YYYYMMDD) to ms UTC at 16:00 ET (21:00 UTC)."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    expiry_date = datetime.strptime(expiry_str, "%Y%m%d")
    expiry_close_et = expiry_date.replace(hour=16, minute=0, second=0, tzinfo=et)
    return int(expiry_close_et.timestamp() * 1000)


def _ttm_years(snapshot_ms: int, expiry_ms: int) -> float:
    return (expiry_ms - snapshot_ms) / (365.25 * 24 * 3600 * 1000)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        import ib_insync as ibi
    except ImportError:
        print("ERROR: ib_insync not installed. Run: pip install ib_insync", file=sys.stderr)
        return 1

    try:
        import pyarrow as pa
    except ImportError:
        print("ERROR: pyarrow not installed. Run: pip install pyarrow", file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    ib = ibi.IB()
    print(f"Connecting to IBKR at {args.host}:{args.port} (clientId={args.client_id}) ...")
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, timeout=15)
    except Exception as exc:
        print(f"ERROR: Could not connect to IBKR: {exc}", file=sys.stderr)
        print("  Check that TWS or IB Gateway is running with API access enabled.", file=sys.stderr)
        return 1

    snapshot_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    print(f"Connected. Snapshot time: {datetime.fromtimestamp(snapshot_ms / 1000, tz=timezone.utc).isoformat()}")

    try:
        # ── Fetch underlying price ────────────────────────────────────────────
        underlying = ibi.Stock(args.symbol, "SMART", "USD")
        ib.qualifyContracts(underlying)
        und_ticker = ib.reqMktData(underlying, "", False, False)
        ib.sleep(2.0)
        spot = und_ticker.last if und_ticker.last and und_ticker.last > 0 else und_ticker.close
        if not spot or spot <= 0:
            print("ERROR: Could not get underlying price.", file=sys.stderr)
            return 1
        print(f"  {args.symbol} spot: {spot:.2f}")
        ib.cancelMktData(underlying)

        # ── Fetch option chain parameters ─────────────────────────────────────
        chains = ib.reqSecDefOptParams(args.symbol, "", "STK", und_ticker.contract.conId)
        if not chains:
            print("ERROR: No option chain parameters returned.", file=sys.stderr)
            return 1

        # Use SMART exchange with most expirations
        chain = max(chains, key=lambda c: len(c.expirations))
        print(f"  Exchange: {chain.exchange}, expirations: {len(chain.expirations)}, strikes: {len(chain.strikes)}")

        # ── Filter expirations ────────────────────────────────────────────────
        now_ms = snapshot_ms
        valid_expiries = []
        for exp_str in sorted(chain.expirations):
            exp_ms = _expiry_close_ms(exp_str)
            days = (exp_ms - now_ms) / (24 * 3600 * 1000)
            if args.min_days <= days <= args.max_days:
                valid_expiries.append(exp_str)

        print(f"  Valid expirations ({args.min_days}-{args.max_days}d): {valid_expiries}")
        if not valid_expiries:
            print("ERROR: No expirations in the desired window.", file=sys.stderr)
            return 1

        # ── Filter strikes ────────────────────────────────────────────────────
        lo = spot * (1 - args.moneyness_pct / 100)
        hi = spot * (1 + args.moneyness_pct / 100)
        valid_strikes = [k for k in chain.strikes if lo <= k <= hi]
        print(f"  Valid strikes (±{args.moneyness_pct}% of {spot:.2f}): {len(valid_strikes)} strikes")

        # ── Request market data for each contract ─────────────────────────────
        contracts: list[ibi.Option] = []
        for exp in valid_expiries:
            for strike in valid_strikes:
                for right in ("C", "P"):
                    contracts.append(
                        ibi.Option(args.symbol, exp, strike, right, "SMART")
                    )

        print(f"  Qualifying {len(contracts)} contracts ...")
        ib.qualifyContracts(*contracts)
        contracts = [c for c in contracts if c.conId]  # drop unqualified
        print(f"  Qualified: {len(contracts)}")

        # Request option market data. Empty genericTickList causes TWS to return
        # modelGreeks automatically for options (including optPrice and impliedVol).
        # Tick 225 is auction data, not option Greeks.
        tickers: list[ibi.Ticker] = []
        BATCH = 50
        for i in range(0, len(contracts), BATCH):
            batch = contracts[i : i + BATCH]
            for c in batch:
                t = ib.reqMktData(c, "", False, False)
                tickers.append(t)
            ib.sleep(3.0)

        print("  Waiting for market data ...")
        ib.sleep(5.0)

        # ── Collect rows ──────────────────────────────────────────────────────
        rows: list[dict] = []
        for t, c in zip(tickers, contracts):
            # Reject missing, one-sided, or crossed quotes.
            if t.bid is None or t.ask is None:
                continue
            bid = float(t.bid)
            ask = float(t.ask)
            if bid < 0.05 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2.0

            # Oracle: IBKR model IV and the model price it was backed out from.
            if not (t.modelGreeks and t.modelGreeks.impliedVol and t.modelGreeks.impliedVol > 0):
                continue
            ibkr_iv = float(t.modelGreeks.impliedVol)
            if not (0.05 <= ibkr_iv <= 2.0):
                continue
            if not (t.modelGreeks.optPrice and t.modelGreeks.optPrice > 0):
                continue
            ibkr_model_price = float(t.modelGreeks.optPrice)

            exp_ms = _expiry_close_ms(c.lastTradeDateOrContractMonth)
            ttm = _ttm_years(snapshot_ms, exp_ms)
            if ttm <= 0:
                continue

            rows.append({
                "symbol": c.symbol,
                "right": c.right,
                "strike": float(c.strike),
                "expiry_ms": exp_ms,
                "snapshot_ms": snapshot_ms,
                "spot": float(spot),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "ibkr_model_price": ibkr_model_price,
                "ibkr_iv": ibkr_iv,
                "ttm_years": ttm,
                "rate": args.rate,
                "dividend": args.div,
                "is_call": c.right == "C",
            })

        for t in tickers:
            ib.cancelMktData(t.contract)

    finally:
        ib.disconnect()

    print(f"\n  Captured {len(rows)} contracts after filtering.")
    if not rows:
        print("ERROR: No rows survived filtering. Try loosening --moneyness-pct or --min-days.", file=sys.stderr)
        return 1

    # ── Write Arrow file ──────────────────────────────────────────────────────
    table = pa.table({
        "symbol": pa.array([r["symbol"] for r in rows], type=pa.string()),
        "right": pa.array([r["right"] for r in rows], type=pa.string()),
        "strike": pa.array([r["strike"] for r in rows], type=pa.float64()),
        "expiry_ms": pa.array([r["expiry_ms"] for r in rows], type=pa.int64()),
        "snapshot_ms": pa.array([r["snapshot_ms"] for r in rows], type=pa.int64()),
        "spot": pa.array([r["spot"] for r in rows], type=pa.float64()),
        "bid": pa.array([r["bid"] for r in rows], type=pa.float64()),
        "ask": pa.array([r["ask"] for r in rows], type=pa.float64()),
        "mid": pa.array([r["mid"] for r in rows], type=pa.float64()),
        "ibkr_model_price": pa.array([r["ibkr_model_price"] for r in rows], type=pa.float64()),
        "ibkr_iv": pa.array([r["ibkr_iv"] for r in rows], type=pa.float64()),
        "ttm_years": pa.array([r["ttm_years"] for r in rows], type=pa.float64()),
        "rate": pa.array([r["rate"] for r in rows], type=pa.float64()),
        "dividend": pa.array([r["dividend"] for r in rows], type=pa.float64()),
        "is_call": pa.array([r["is_call"] for r in rows], type=pa.bool_()),
    })

    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"snapshot_{ts_str}.arrow"

    sink = pa.ipc.new_file(str(out_path), table.schema)
    sink.write_table(table)
    sink.close()

    print(f"\nSnapshot written: {out_path}")
    print(f"  Contracts: {len(rows)}")
    print(f"  Symbols:   {sorted(set(r['symbol'] for r in rows))}")
    expiries = sorted(set(r['expiry_ms'] for r in rows))
    print(f"  Expirations: {len(expiries)} unique")
    print(f"  IBKR IV range: {min(r['ibkr_iv'] for r in rows):.4f} – {max(r['ibkr_iv'] for r in rows):.4f}")
    print()
    print("Next step:")
    print(f"  python scripts/generate_fixtures.py --id OPT-IB-002 --justification 'Initial IBKR IV fixture, snapshot {ts_str}'")

    return 0


if __name__ == "__main__":
    sys.exit(main())
