"""LEAN Sidecar Lab — Phase 1 runner spike.

Authority: ADR 0070.

This package owns the data-plane glue for the LEAN Sidecar Lab: workspace
staging, manifest writing, trusted-sample wiring, and the launcher service
that owns the Podman API access. User `QCAlgorithm` code never executes
in this process; it executes only inside a disposable, network-isolated
container invoked by the launcher.
"""
