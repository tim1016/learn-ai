"""In-app help serves byte copies of three repo documents; each pair stays identical."""

from __future__ import annotations

from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

# The app serves these from `Frontend/src/assets/docs/` (routes in
# `Frontend/src/app/app.routes.ts`). Edit the repo document, then copy it over.
SERVED_DOCUMENT_COPIES = {
    "docs/architecture-manual.md": "Frontend/src/assets/docs/architecture-manual.md",
    "docs/indicator-reliability-methodology.md": (
        "Frontend/src/assets/docs/indicator-reliability-methodology.md"
    ),
    "docs/signal-engine-authority.md": "Frontend/src/assets/docs/signal-engine-methodology.md",
}


@pytest.mark.parametrize(("canonical", "served"), sorted(SERVED_DOCUMENT_COPIES.items()))
def test_served_document_copy_matches_its_repo_document(canonical: str, served: str) -> None:
    canonical_bytes = (REPOSITORY_ROOT / canonical).read_bytes()
    served_bytes = (REPOSITORY_ROOT / served).read_bytes()

    assert served_bytes == canonical_bytes, (
        f"{served} differs from {canonical}; copy the repo document over the served one"
    )
