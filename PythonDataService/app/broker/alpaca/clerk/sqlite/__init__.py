"""Event-sourced SQLite authority for the Alpaca Account Clerk (#1375+).

Import the submodule you need (``repository``, ``reads``, ``schema``, …);
the package itself exports nothing. SQL stays behind the repository boundary
and never spreads through routers, strategy, or presentation code.
"""
