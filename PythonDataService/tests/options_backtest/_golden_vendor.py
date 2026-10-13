"""The vendor's recorded answers, served to the lake's real fetchers, for the Options Backtest's golden fixtures (#3110).

A golden fixture of this feature keeps the vendor's real answers byte for byte, in a folder of files named by the request
each one answers. ``recorded_vendor`` serves those bytes through ``respx`` to the lake's own fetchers, and any request for
a path the recording does not hold fails. It asks one more thing of every option request: the key rode in the
``Authorization: Bearer`` header and in no URL. The stock pipeline's request for the underlying is not asked that: that
older fetcher puts the key in the URL, which is known and not part of a fixture.

The suite's lake fixture gives the settings the key ``THE_KEY``, and the LEAN launcher the stock pipeline asks for the
exchange calendar is served by the suite's stand-in.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import httpx
import respx

from tests._helpers.fake_lake_catalog import launcher_responder

# The key the lake's fetchers hold in these tests (the suite's lake fixture sets it), as the bearer token they must send.
THE_KEY = "test-key"


def the_recorded_answer(
    recording: Path, files: Mapping[str, str], stock_path: str, path: str
) -> Callable[[httpx.Request], httpx.Response]:
    """The vendor's recorded bytes for ``path``, after checking how the request carried the key.

    ``files`` maps each request path to the file in ``recording`` that holds the bytes the vendor answered it with.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        if path != stock_path:
            assert request.headers.get("authorization") == f"Bearer {THE_KEY}", (
                f"the key is not in the header of {path}"
            )
            assert "apikey" not in {param.lower() for param in request.url.params}, f"the key is in the URL of {path}"
        return httpx.Response(
            200, content=(recording / files[path]).read_bytes(), headers={"content-type": "application/json"}
        )

    return answer


@contextmanager
def recorded_vendor(recording: Path, files: Mapping[str, str], stock_path: str) -> Iterator[respx.MockRouter]:
    """The vendor, answering each recorded request with the recorded bytes. A request for any other path fails."""
    with respx.mock(assert_all_called=False) as router:
        for path in files:
            router.get(host="api.polygon.io", path=path).mock(
                side_effect=the_recorded_answer(recording, files, stock_path, path)
            )
        router.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(side_effect=launcher_responder())
        yield router


def requests_to_the_vendor(router: respx.MockRouter, stock_path: str) -> list[tuple[str, dict[str, str]]]:
    """Each request made to Polygon, as its path and its query (the stock request's key left out, as a key is no fact)."""
    return [
        (
            call.request.url.path,
            {k: v for k, v in call.request.url.params.items() if call.request.url.path != stock_path or k != "apiKey"},
        )
        for call in router.calls
        if call.request.url.host == "api.polygon.io"
    ]
