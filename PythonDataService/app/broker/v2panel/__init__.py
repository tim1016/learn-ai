"""Broker-v2 bot control panel — contracts and read/action projections (S1).

Everything here is backend-authored and broker-generic. The panel surface is a
projection over the S0 evidence (decision journal, attributed fills, FIFO P&L,
rollup caches) plus the durable bot lifecycle artifacts and the Alpaca clerk's
journal-derived state. Angular renders strictly from these contracts (spec §3-§13).
"""
