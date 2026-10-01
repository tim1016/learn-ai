# LEAN sidecar launcher — operator runbook

**Purpose:** make the LEAN sidecar launcher runnable on a host and prove the data plane can reach it.

**Authority:** [ADR 0070](../architecture/adrs/0070-lean-sidecar-boundary.md) holds why the launcher is a separate host process, the image pin policy and the container boundary. The launcher start command is in [`PythonDataService/CLAUDE.md`](../../PythonDataService/CLAUDE.md) ("LEAN Sidecar launcher").

## Local-image readiness is mandatory

The pinned LEAN image (its digest is in
`PythonDataService/app/lean_sidecar/config.py`) must be present in the
launcher's *local* Podman image store. The launcher checks it with
`podman image exists` as part of `/healthz`; a reachable HTTP process alone
is not runnable readiness. If the derivative was pruned or never built,
Podman tries to pull this local-only reference from `localhost` and exits
125. Rebuild with the `podman build` command in
[`PythonDataService/lean_sidecar/Dockerfile.arm64-dotnet109`](../../PythonDataService/lean_sidecar/Dockerfile.arm64-dotnet109),
update the arm64 pin from the resulting local digest
([`PythonDataService/scripts/lean_sidecar_pin_image.py`](../../PythonDataService/scripts/lean_sidecar_pin_image.py)
rewrites it in `config.py`), and restart the launcher before submitting a
LEAN run.

## Verifying the data-plane → launcher path (new machine, fresh clone)

The launcher binds on the host (Windows / Linux); the data plane runs
inside `polygon-data-service`. The container reaches the host launcher
via the `host.containers.internal` alias, registered on the
`python-service` service in `compose.yaml` as
`extra_hosts: - "host.containers.internal:host-gateway"`.
`host-gateway` resolves to the container's default gateway — the host
on Windows and Linux Podman — so the compose default
`LEAN_LAUNCHER_URL=http://host.containers.internal:8090` works without
per-machine tuning. `host.docker.internal` is registered too for
Docker/Desktop compatibility and older local envs, but Podman users
should prefer `host.containers.internal`.

Verification on a fresh clone / new machine:

1. Start the launcher on the host (binding `--host 0.0.0.0`, per
   `PythonDataService/CLAUDE.md`). The launcher generates an auth
   token and writes it to the artifacts root the container will
   bind-mount.
2. `./restart.sh` to bring compose up.
3. From the host, hit the data-plane diagnostics endpoint:
   ```bash
   curl -s http://localhost:8000/api/lean-sidecar/diagnose | jq
   ```
   Expect `overall_status: "pass"` with five rows: `launcher_url`,
   `launcher_url_parseable`, `launcher_token`, `launcher_healthz`, and
   `launcher_image`. The last row confirms the exact pinned image exists
   locally; a 200 response from the launcher without that image is a
   blocked, non-runnable state.

The diagnostics endpoint is read-only — it probes the launcher's
unauthenticated `/healthz`, which performs only `podman image exists`,
and inspects the local token-resolution path; it never spawns a sidecar
run. The endpoint is implemented in
`PythonDataService/app/lean_sidecar/diagnostics.py` and exposed by the
`/api/lean-sidecar/*` router. Override `LEAN_LAUNCHER_URL` only for
non-standard runtimes (remote launcher, non-default port); the default
should not need machine-specific tweaks. Machine-specific LAN IPs such
as `192.168.x.x`, `10.x.x.x`, or `172.16-31.x.x` are intentionally
diagnosed as brittle because they change with network attachment and
are often blocked by host firewalls.

## Windows + Podman topology

On a Windows host with Podman, "host-side" cannot assume the Linux layout
where `/var/run/podman/podman.sock` and the workspace path live in the same
namespace. The Podman API may live inside the WSL2/podman-machine VM, while
the repo lives under `C:\Users\...` and is presented to containers through a
VM mount.

Before a Windows host accepts caller-supplied algorithm source, check:

- Decide where the launcher runs on Windows: native Windows process,
  WSL2/podman-machine process, or a small launcher container with only the
  Podman socket and artifacts root mounted.
- Prove host path resolution from `run_id` to the exact path mounted into the
  LEAN container. The data-plane still never sends arbitrary paths.
- Prove UID/GID behavior for the mounted workspace: LEAN must be able to write
  `output/` as the `--user` the runner passes
  (`PythonDataService/app/lean_sidecar/runner.py`, `_container_user_spec`).
- Record the Windows topology in this runbook and in the launcher test fixture.

Until those checks pass on a host, run only the bundled trusted samples there.
