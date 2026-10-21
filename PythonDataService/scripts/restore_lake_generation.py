"""Restore a verified lake/catalog pair into empty targets; starts no service.

This self-contained file is copied into each generation. It needs Python 3.12
and asyncpg, already present in the data-service image. Credentials are read
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
from pathlib import Path, PurePosixPath
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify(generation: Path) -> tuple[dict[str, Any], Path]:
    raw = (generation / "manifest.json").read_bytes()
    if (generation / "COMPLETE").read_text() != digest(raw):
        raise ValueError("generation is not complete or its manifest changed")
    manifest = json.loads(raw)
    if manifest["version"] != 1 or manifest["generation_id"] != generation.name:
        raise ValueError("generation version or identity differs")
    if digest((generation / "catalog.copy").read_bytes()) != manifest["catalog_sha256"]:
        raise ValueError("catalog dump checksum differs")
    objects = generation.parent.parent / "objects"
    for file in manifest["files"]:
        path = PurePosixPath(file["relative_path"])
        if path.is_absolute() or any(part in ("", ".", "..") for part in file["relative_path"].split("/")):
            raise ValueError("unsafe backup file path")
        data = (objects / file["sha256"]).read_bytes()
        if len(data) != file["size_bytes"] or digest(data) != file["sha256"]:
            raise ValueError("backup file checksum differs")
    for root_id, checksum in manifest["root_markers"].items():
        if not any(file["root_id"] == root_id and file["relative_path"] == "lake/.data-root.json" and file["sha256"] == checksum
                   for file in manifest["files"]):
            raise ValueError("file inventory is missing its exact root marker")
        raw_marker = (objects / checksum).read_bytes()
        marker = json.loads(raw_marker)
        if digest(raw_marker) != checksum or marker.get("data_root_id") != root_id or marker.get("schema_version") != 1:
            raise ValueError("root marker identity differs")
    return manifest, objects


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generation", type=Path)
    parser.add_argument("--root", action="append", required=True, help="ROOT_UUID=/empty/target/base-root")
    parser.add_argument("--dsn-env", default="POSTGRES_URL")
    parser.add_argument("--schema", default="public")
    args = parser.parse_args()
    if not args.schema.replace("_", "").isalnum() or not args.schema.isascii():
        raise ValueError("unsafe target schema")
    manifest, objects = verify(args.generation)
    roots = {entry.split("=", 1)[0]: Path(entry.split("=", 1)[1]) for entry in args.root}
    if set(roots) != set(manifest["root_ids"]):
        raise ValueError("every backed-up physical root needs an explicit target")
    for root in roots.values():
        if root.exists() and any(root.iterdir()):
            raise ValueError("target roots must be empty")
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise ValueError("target catalog credential is missing from its environment variable")
    connection = await asyncpg.connect(dsn, command_timeout=120)
    try:
        columns = await connection.fetch('''SELECT column_name, data_type, udt_name, is_nullable, ordinal_position
            FROM information_schema.columns WHERE table_schema=$1 AND table_name='DataLakeArtifacts'
            ORDER BY ordinal_position''', args.schema)
        schema_hash = digest(json.dumps([dict(row) for row in columns], sort_keys=True, separators=(",", ":")).encode())
        if schema_hash != manifest["schema_hash"]:
            raise ValueError("target catalog schema differs; use the matching release and migrations")
        migrations = await connection.fetch('SELECT "MigrationId" FROM public."__EFMigrationsHistory" ORDER BY "MigrationId"')
        if [row["MigrationId"] for row in migrations] != manifest["migration_ids"]:
            raise ValueError("target catalog migrations differ from the generation")
        if await connection.fetchval(f'SELECT COUNT(*) FROM "{args.schema}"."DataLakeArtifacts"'):
            raise ValueError("target catalog must be empty")
        # All checks above finish before creating a single target file.
        for file in manifest["files"]:
            target = roots[file["root_id"]] / file["relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write((objects / file["sha256"]).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        async with connection.transaction():
            await connection.copy_to_table("DataLakeArtifacts", schema_name=args.schema,
                source=io.BytesIO((args.generation / "catalog.copy").read_bytes()), format="binary")
            await connection.execute(f'''UPDATE "{args.schema}"."DataLakeArtifacts" SET
                "LeaseOwner"=NULL, "LeaseExpiresAtMs"=NULL, "LeaseGeneration"="LeaseGeneration"+1,
                "Status"=CASE WHEN "Status"='fetching' THEN 'failed' ELSE "Status" END,
                "LastError"=CASE WHEN "Status"='fetching' THEN 'internal_error' ELSE "LastError" END,
                "ErrorMessage"=CASE WHEN "Status"='fetching' THEN 'Interrupted at backup; restore starts no jobs' ELSE "ErrorMessage" END''')
            sequence = await connection.fetchval("SELECT pg_get_serial_sequence($1, 'Id')", f'"{args.schema}"."DataLakeArtifacts"')
            if sequence is not None:
                maximum = await connection.fetchval(f'SELECT MAX("Id") FROM "{args.schema}"."DataLakeArtifacts"')
                await connection.fetchval("SELECT setval($1::regclass, $2, $3)", sequence, maximum or 1, maximum is not None)
        logger.info("Lake generation restored; services remain stopped", extra={"action": "lake_generation_restored", "generation_id": manifest["generation_id"]})
    finally:
        await connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
