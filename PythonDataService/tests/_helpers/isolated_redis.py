"""A dedicated disposable Redis for tests that need the real job-store scripts."""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
import redis


@pytest.fixture(scope="module")
def isolated_redis() -> Iterator[str]:
    """A dedicated disposable service, never the developer's shared job store."""
    if shutil.which("podman") is None:
        pytest.skip("real socket boundary tests need Podman for a disposable Redis")
    name = f"options-http-{uuid.uuid4().hex[:12]}"
    subprocess.run(["podman", "run", "--rm", "-d", "--name", name, "-p", "127.0.0.1::6379",
                    "docker.io/library/redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no"],
                   check=True, capture_output=True, text=True)
    try:
        address = subprocess.run(["podman", "port", name, "6379"], check=True, capture_output=True, text=True).stdout.strip()
        url = f"redis://{address}/0"
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        deadline = time.monotonic() + 10
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield url
    finally:
        subprocess.run(["podman", "rm", "-f", name], check=True, capture_output=True, text=True)
