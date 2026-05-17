"""Launcher service.

Separate from the data-plane (``polygon-data-service``) for the reasons
laid out in ``docs/architecture/lean-sidecar-lab.md`` §"Launcher
topology". This package exposes:

* a Pydantic request model
* a request validator that re-asserts the artifacts-root boundary, the
  image digest allow-list, and run-limit positivity
* a FastAPI app exposing exactly one route, ``POST /launch``
* a CLI entry point for running the launcher as a host process

Nothing in this package executes user-supplied source. Source is
already on disk in the workspace by the time ``POST /launch`` arrives;
the launcher only invokes the container.
"""
