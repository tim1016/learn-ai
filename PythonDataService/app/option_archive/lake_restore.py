"""Verify a lake/catalog backup generation and restore it into empty targets.

The weekly restore check and an operator's recovery run this same code. Each
generation carries a copy of this file as ``restore_lake_generation.py``, so a
recovery survives losing the checkout: it needs only Python 3.12 and asyncpg,
never ``app``. It starts no service. The target catalog credential is read
only from the named environment variable, never from a command argument.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# The lake's root-identity marker (app.data_lake.root_identity), restated
# because this file cannot import ``app``; a parity test pins the two together.
MARKER_PATH = "lake/.data-root.json"
MARKER_SCHEMA_VERSION = 1
INCOMPLETE_MARKER = ".restore-incomplete.json"


class RestoreRefusedError(ValueError):
    """The generation or its targets cannot establish a recoverable lake/catalog pair."""


def _continue() -> None:
    """The checkpoint of a run that nothing pauses."""


async def _inline(work: Callable[[Callable[[], None]], Any]) -> Any:
    return work(_continue)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def write_durable(path: Path, payload: bytes) -> None:
    """Create ``path`` exclusively, then make its bytes and its directory entry durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def safe_relative(text: str) -> Path:
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in text.split("/")):
        raise RestoreRefusedError("backup contains an unsafe relative file path")
    return Path(*path.parts)


def verify(generation: Path, *, expected_schema_hash: str | None = None,
           checkpoint: Callable[[], None] = _continue) -> dict[str, Any]:
    """Check COMPLETE, the catalog dump, every object and each root's marker; return the manifest."""
    raw = (generation / "manifest.json").read_bytes()
    if (generation / "COMPLETE").read_text() != digest(raw):
        raise RestoreRefusedError("generation has no valid complete marker")
    manifest = json.loads(raw)
    try:
        _verify_manifest(generation, manifest, expected_schema_hash, checkpoint)
    except (KeyError, TypeError) as exc:
        raise RestoreRefusedError(f"generation manifest is malformed: {exc!r}") from exc
    return manifest


def _verify_manifest(generation: Path, manifest: dict[str, Any], expected_schema_hash: str | None,
                     checkpoint: Callable[[], None]) -> None:
    if manifest["version"] != 1 or generation.name.lstrip(".") != manifest["generation_id"]:
        raise RestoreRefusedError("generation identity or version is invalid")
    if expected_schema_hash is not None and manifest["schema_hash"] != expected_schema_hash:
        raise RestoreRefusedError("restore schema differs from the backed-up schema")
    if digest((generation / "catalog.copy").read_bytes()) != manifest["catalog_sha256"]:
        raise RestoreRefusedError("catalog dump checksum is invalid")
    objects = generation.parent.parent / "objects"
    seen: set[tuple[str, str]] = set()
    markers: dict[str, bytes] = {}
    for item in manifest["files"]:
        checkpoint()
        safe_relative(item["relative_path"])
        key = (item["root_id"], item["relative_path"])
        if item["root_id"] not in manifest["root_ids"] or key in seen:
            raise RestoreRefusedError("generation contains duplicate or foreign-root files")
        seen.add(key)
        payload = (objects / item["sha256"]).read_bytes()
        if len(payload) != item["size_bytes"] or digest(payload) != item["sha256"]:
            raise RestoreRefusedError("backup file checksum or size is invalid")
        if item["relative_path"] == MARKER_PATH:
            markers[item["root_id"]] = payload
    for root_id in manifest["root_ids"]:
        marker = json.loads(markers[root_id]) if root_id in markers else None
        if (not isinstance(marker, dict) or marker.get("schema_version") != MARKER_SCHEMA_VERSION
                or marker.get("data_root_id") != root_id):
            raise RestoreRefusedError("backup root marker is missing or has the wrong identity")
    for object_digest in manifest["restore_files"].values():
        if digest((objects / object_digest).read_bytes()) != object_digest:
            raise RestoreRefusedError("backup restore tooling is corrupt")


async def catalog_schema_hash(connection: Any, *, schema: str = "public") -> str:
    columns = await connection.fetch('''SELECT column_name, data_type, udt_name, is_nullable, ordinal_position
        FROM information_schema.columns WHERE table_schema=$1 AND table_name='DataLakeArtifacts'
        ORDER BY ordinal_position''', schema)
    if not columns:
        raise RestoreRefusedError("catalog table is missing")
    return digest(canonical([dict(row) for row in columns]))


async def restore(generation: Path, roots: Mapping[str, Path], connection: Any, *, schema: str = "public",
                  checkpoint: Callable[[], None] = _continue,
                  run_files: Callable[[Callable[[Callable[[], None]], Any]], Awaitable[Any]] = _inline) -> dict[str, Any]:
    """Restore exact file bytes and the binary catalog dump together; return the manifest.

    ``run_files`` runs each blocking file pass, handing it the checkpoint to
    call between files. ``checkpoint`` is also called before the catalog import
    commits. Leases are cleared, old generations fenced and unfinished fetches
    marked failed; nothing is reindexed, rehashed or fetched.
    """
    if not schema.replace("_", "").isalnum() or not schema.isascii():
        raise RestoreRefusedError("unsafe restore catalog schema")
    expected = await catalog_schema_hash(connection, schema=schema)
    manifest = await run_files(lambda paused: verify(generation, expected_schema_hash=expected, checkpoint=paused))
    migrations = await connection.fetch('SELECT "MigrationId" FROM public."__EFMigrationsHistory" ORDER BY "MigrationId"')
    if [row["MigrationId"] for row in migrations] != manifest["migration_ids"]:
        raise RestoreRefusedError("restore migration history differs from the generation")
    if set(roots) != set(manifest["root_ids"]):
        raise RestoreRefusedError("restore targets do not match the generation's root identities")
    for target in roots.values():
        if target.exists() and any(target.iterdir()):
            raise RestoreRefusedError("restore requires fresh, empty target roots")
    if await connection.fetchval(f'SELECT COUNT(*) FROM "{schema}"."DataLakeArtifacts"'):
        raise RestoreRefusedError("restore requires an empty target catalog")
    objects = generation.parent.parent / "objects"

    # All verification happens before the first target mutation. A failure
    # thereafter leaves an explicitly incomplete target, never a live service.
    def write_files(paused: Callable[[], None]) -> None:
        paused()
        for target in roots.values():
            write_durable(target / INCOMPLETE_MARKER, canonical({"generation_id": manifest["generation_id"]}))
        for item in manifest["files"]:
            paused()
            write_durable(roots[item["root_id"]] / safe_relative(item["relative_path"]),
                          (objects / item["sha256"]).read_bytes())
    await run_files(write_files)
    checkpoint()
    async with connection.transaction():
        await connection.copy_to_table("DataLakeArtifacts", schema_name=schema,
            source=io.BytesIO((generation / "catalog.copy").read_bytes()), format="binary")
        await connection.execute(f'''UPDATE "{schema}"."DataLakeArtifacts" SET "LeaseOwner"=NULL,
            "LeaseExpiresAtMs"=NULL, "LeaseGeneration"="LeaseGeneration"+1,
            "Status"=CASE WHEN "Status"='fetching' THEN 'failed' ELSE "Status" END,
            "LastError"=CASE WHEN "Status"='fetching' THEN 'internal_error' ELSE "LastError" END,
            "ErrorMessage"=CASE WHEN "Status"='fetching' THEN 'Interrupted at backup; restore starts no jobs' ELSE "ErrorMessage" END''')
        sequence = await connection.fetchval("SELECT pg_get_serial_sequence($1, 'Id')", f'"{schema}"."DataLakeArtifacts"')
        if sequence is not None:
            maximum = await connection.fetchval(f'SELECT MAX("Id") FROM "{schema}"."DataLakeArtifacts"')
            await connection.fetchval("SELECT setval($1::regclass, $2, $3)", sequence, maximum or 1, maximum is not None)
        checkpoint()
    for target in roots.values():
        (target / INCOMPLETE_MARKER).unlink()
    return manifest


async def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation", type=Path)
    parser.add_argument("--root", action="append", required=True, help="ROOT_UUID=/empty/target/base-root")
    parser.add_argument("--dsn-env", default="POSTGRES_URL")
    parser.add_argument("--schema", default="public")
    args = parser.parse_args(argv)
    roots = {entry.split("=", 1)[0]: Path(entry.split("=", 1)[1]) for entry in args.root}
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise RestoreRefusedError("target catalog credential is missing from its environment variable")
    connection = await asyncpg.connect(dsn, command_timeout=120)
    try:
        manifest = await restore(args.generation, roots, connection, schema=args.schema)
    finally:
        await connection.close()
    logger.info("Lake generation restored; services remain stopped",
                extra={"action": "lake_generation_restored", "generation_id": manifest["generation_id"]})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
