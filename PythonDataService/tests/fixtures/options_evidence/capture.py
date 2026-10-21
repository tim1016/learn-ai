"""Capture the retained read fixtures described in attribution.md; no replay commands."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.request import urlopen

from app.research import options_candidate_evidence as reader
from app.research.options_golden_search import repository
from app.research.options_golden_search.projections import session_detail
from app.research.options_golden_search.protocol import OptionsSearchProtocol
from app.research.options_golden_search.stages import default_blob_store
from app.research.persistence.db import with_connection
from app.schemas.options_golden_search import StudyDetail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=False)
    routes = {"run76": "/api/research/options-backtest/runs/76", "run69": "/api/research/options-backtest/runs/69",
        "golden76": "/api/research/golden-option-setups/ogs_dbe79f85399048f4a4decc3c86e1f3ff"}
    for name, route in routes.items():
        with urlopen(args.base_url + route, timeout=30) as response:
            value = json.load(response)
        if name == "golden76":
            value["proof"] = {}
        (args.destination / f"{name}.json").write_text(json.dumps(value, separators=(",", ":")) + "\n")
    value = asyncio.run(with_connection(repository.get, "os_d7b4752086147e437a3a0a56ecc980f33d3c6055"))
    (args.destination / "study-record.json").write_text(json.dumps(value, separators=(",", ":")) + "\n")
    digest = reader.setup_sha256(OptionsSearchProtocol.model_validate(value["protocol"]).setup)
    key = reader.resolve(value, digest, "development")[3]
    record = asyncio.run(with_connection(repository.get_evaluation, value["id"], key))
    blobs = default_blob_store()
    retained = reader.load_record(value, record, digest, "development", blobs=blobs)
    projections = {"development-evidence": {"record": record, "payload": json.loads(blobs.get(record["result"]["evidence_blob"]))},
        "candidate-development": reader.project(retained).model_dump(mode="json"),
        "study-summary": StudyDetail.from_record(value).model_dump(mode="json"),
        "candidate-session": next(detail for session in retained.result.sessions if (detail := session_detail(session))["attempts"])}
    for name, projection in projections.items():
        (args.destination / f"{name}.json").write_text(json.dumps(projection, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
