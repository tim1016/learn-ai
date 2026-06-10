# Derivative LEAN image used by the LEAN Sidecar launcher.
#
# Why this exists: the upstream quantconnect/lean image we pinned ships
# /root with mode 0700, which is incompatible with the launcher's
# `--user=10001:10001 --read-only` sandbox (added in Phase 1c, see
# `app/lean_sidecar/runner.py`). UID 10001 cannot traverse /root, so
# /root/.dotnet/dotnet fails to exec with EACCES. We open just the
# traversal bit on /root; nothing else is modified.
#
# Build:
#   podman build \
#     -t learn-ai/lean-sandbox:phase-1c \
#     -f PythonDataService/lean_sidecar/Dockerfile \
#     PythonDataService/lean_sidecar
#
# After building, capture the local digest and pin it in
# `app/lean_sidecar/config.py` (PINNED_LEAN_IMAGE_DIGEST). The launcher
# rejects any image whose digest is not in ALLOWED_IMAGE_DIGESTS, so the
# tag above is purely human-readable.
#
# The FROM is pinned by digest, not :latest, so this build is
# bit-deterministic w.r.t. upstream. Upgrades are deliberate: bump the
# digest below, rebuild, capture the new derivative digest, update
# config.py + the ADR in one PR.
FROM docker.io/quantconnect/lean@sha256:4934c22c2b080a688f25b571746603e01533c5e581499d8457e5624a132ba77b

RUN chmod 0755 /root
