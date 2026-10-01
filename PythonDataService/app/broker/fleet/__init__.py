"""The broker clerk fleet control plane (ADR 0062).

A narrow, broker-neutral Python coordinator: clerk identity, discovery,
broker-qualified account-assignment fencing, routing correlation and health
projection. It is not an execution authority. Its registry stores no lane
configuration, custody, order, fill, position, activation or arming data, and
nothing in this package computes a balance, a position, P&L, exposure or risk.

Execution stays in provider-owned clerk agents — one clerk, one process, one
distinct physical volume. The production provider-adapter registry is
code-owned and declares no providers in this slice; the Alpaca adapter and the
coordinator/agent role split land in a later phase, and test-only fake
adapters inject through constructor injection without ever entering the
production mapping.

No module in this package may import an Alpaca risk, custody, execution, arming
or recovery implementation (ADR 0062 Decision 6);
``tests/broker/fleet/test_import_isolation.py`` asserts it.
"""
