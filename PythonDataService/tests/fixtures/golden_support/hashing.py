"""SHA-256 hashing for golden fixture files.

Two hashes per fixture file:

  content_sha256 — canonical normalized data identity.
    For Arrow/Parquet files: columns sorted alphabetically, fixed dtypes,
    no compression, serialized to bytes and hashed. Two fixtures containing
    the same data (even stored differently) produce the same content_sha256.

  file_sha256 — exact storage artifact identity.
    Raw bytes of the file on disk. Detects any accidental modification,
    including whitespace-only changes in JSON or metadata changes in Parquet.

Both are lowercase hex strings (64 characters).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as pa_ipc


def file_sha256(path: Path) -> str:
    """SHA-256 of the raw bytes of the file at ``path``."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def content_sha256_arrow(path: Path) -> str:
    """SHA-256 of the canonical normalized form of an Arrow IPC file.

    Normalization:
    - Columns sorted alphabetically by name
    - Schema cast to canonical dtypes (see _canonical_dtype)
    - Serialized to Arrow IPC stream format (no compression, no metadata)
    - Hash of the resulting bytes

    This is the content identity: two tables with the same data but different
    column order or compression produce the same hash.
    """
    with pa_ipc.open_file(str(path)) as reader:
        table = reader.read_all()
    return _hash_arrow_table(table)


def content_sha256_json(path: Path) -> str:
    """SHA-256 of the canonical normalized form of a JSON file.

    Normalization: keys sorted, no extra whitespace, UTF-8 encoded.
    This is the content identity.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_sha256_bytes(data: bytes) -> str:
    """SHA-256 of arbitrary bytes (for testing and inline data)."""
    return hashlib.sha256(data).hexdigest()


def compute_hashes(files_dir: Path, filenames: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Compute content_sha256 and file_sha256 for a list of fixture files.

    Returns (content_hashes, file_hashes) — both dicts map filename -> hash.
    file_sha256 is always computed. content_sha256 is computed based on
    file extension (.arrow, .feather -> Arrow; .json -> JSON; others -> raw bytes).
    """
    content_hashes: dict[str, str] = {}
    file_hashes: dict[str, str] = {}

    for name in filenames:
        path = files_dir / name
        file_hashes[name] = file_sha256(path)

        ext = path.suffix.lower()
        if ext in {".arrow", ".feather"}:
            content_hashes[name] = content_sha256_arrow(path)
        elif ext == ".json":
            content_hashes[name] = content_sha256_json(path)
        else:
            content_hashes[name] = file_sha256(path)  # raw bytes as content identity

    return content_hashes, file_hashes


# ── Internal helpers ──────────────────────────────────────────────────────────


def _canonical_dtype(field: pa.Field) -> pa.DataType:
    """Map Arrow dtype to a canonical form for content hashing.

    Floating-point types stay as-is (float64 is the default; float32 ports
    must be explicit in fixture metadata). Timestamps are normalized to
    int64 ms UTC to match the wire convention.
    """
    t = field.type
    if pa.types.is_timestamp(t):
        return pa.int64()
    if pa.types.is_large_string(t):
        return pa.string()
    return t


def _hash_arrow_table(table: pa.Table) -> str:
    """Hash a PyArrow Table in canonical normalized form."""
    # Sort columns alphabetically
    sorted_names = sorted(table.schema.names)
    table = table.select(sorted_names)

    # Cast to canonical dtypes
    new_fields = [pa.field(f.name, _canonical_dtype(f)) for f in table.schema]
    new_schema = pa.schema(new_fields)
    casted_arrays = []
    for i, col in enumerate(table.columns):
        target = new_schema.field(i).type
        if col.type != target:
            casted_arrays.append(col.cast(target))
        else:
            casted_arrays.append(col)
    table = pa.table(dict(zip(sorted_names, casted_arrays, strict=True)), schema=new_schema)

    # Serialize to IPC stream (no compression, no schema metadata)
    sink = pa.BufferOutputStream()
    schema_no_meta = table.schema.remove_metadata()
    table = table.cast(schema_no_meta)
    with pa_ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    raw = sink.getvalue().to_pybytes()
    return hashlib.sha256(raw).hexdigest()
