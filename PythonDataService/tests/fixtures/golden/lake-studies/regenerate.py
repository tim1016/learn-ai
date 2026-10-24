"""Record the vendor's real answers for the golden fixtures under ``tests/fixtures/golden/lake-studies`` (#3522).

Fixture paperwork: run by hand, never by a test or by CI. The two recordings were made on 2026-10-24 by a one-off script
that sent the requests this one replays (the same paths and queries, the key in the same header) and saved each body
exactly as received. This script is the tidied form of it and **has not been run**; what ties the files beside
``recording.json`` to that recording is the sha256 of each body there, which the golden registry holds the files to.

Running it records a NEW fixture, not the same bytes again: the vendor's ``request_id`` differs on every call and its data
for a past day can be revised. The figures in ``recording.json``'s ``facts`` and the literals in
``tests/research/test_signal_study_lake_vs_provider.py`` would have to be measured again from the new bytes, and the change
made as a new fixture version.

Read-only: every request is a ``GET``. The key is read from the environment, stripped of surrounding whitespace, sent in an
``Authorization`` header and never printed, logged or written; it is in no URL, so it is in no recorded request, and a
body that holds it is refused. No error this script raises quotes the key.

    POLYGON_API_KEY=<key> python regenerate.py <a fixture's v1 folder> <an empty folder to write into>

It replays every request in the fixture's ``recording.json``, writes each body to the empty folder and writes a
``recording.json`` there whose ``requests`` carry the new sha256 and size. ``compared_with`` and ``facts`` are carried over
unchanged and are the person's to re-derive. Stdout is each body's name, size and sha256, as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.polygon.io"
TIMEOUT_S = 60.0
# What a key may hold to ride in a header: printable ASCII and no space. The HTTP library's error text for a bad header
# value quotes the value, so anything else is refused before a request.
SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")


def key_from_environment() -> str:
    key = os.environ["POLYGON_API_KEY"].strip()
    if SENDABLE_KEY.fullmatch(key) is None:
        raise ValueError("POLYGON_API_KEY is empty, or holds a space or a character that is not printable ASCII")
    return key


def require_an_empty_folder(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} is not an empty folder. Name a folder that is empty or does not exist yet.")


def record(client: httpx.Client, key: str, request: dict[str, Any], out: Path) -> dict[str, Any]:
    path = request["path"]
    try:
        response = client.get(f"{BASE_URL}{path}", params=request["query"], headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as error:
        # The library's text can quote the header it refused; name the path and the kind of failure only.
        raise RuntimeError(f"the request for {path} failed ({type(error).__name__}); nothing was saved") from None
    body = response.content
    if key.encode() in body:
        raise RuntimeError(f"the answer to {path} contains the key; nothing was saved")
    if response.status_code != httpx.codes.OK:
        raise RuntimeError(f"{path} answered HTTP {response.status_code}; a fixture records only good answers")
    (out / request["file"]).write_bytes(body)
    return {
        **request,
        "http_status": response.status_code,
        "fetched_on": date.today().isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }


def main(fixture: Path, out: Path) -> list[dict[str, Any]]:
    recording = json.loads((fixture / "recording.json").read_text(encoding="utf-8"))
    require_an_empty_folder(out)
    key = key_from_environment()
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=TIMEOUT_S) as client:
        requests = [record(client, key, request, out) for request in recording["requests"]]
    (out / "recording.json").write_text(json.dumps({**recording, "requests": requests}, indent=1) + "\n", encoding="utf-8")
    return [{"file": r["file"], "bytes": r["bytes"], "sha256": r["sha256"]} for r in requests]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    json.dump(main(Path(sys.argv[1]), Path(sys.argv[2])), sys.stdout, indent=1)
