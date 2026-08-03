"""Shared pytest fixtures for the SkyCast backend test suite."""

import socket
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import server as skycast_server  # noqa: E402  (path must be set up first)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _reset_shared_state(monkeypatch):
    """Every test starts with clean caches/rate-limit buckets and a fake key.

    The cache, rate limiter, and API key are process-wide singletons in
    server.py, so without this, tests would leak state into one another.
    """
    monkeypatch.setattr(skycast_server, "API_KEY", "test-api-key")
    skycast_server.WEATHER_CACHE._entries.clear()
    skycast_server.WEATHER_RATE_LIMITER._hits.clear()
    yield
    skycast_server.WEATHER_CACHE._entries.clear()
    skycast_server.WEATHER_RATE_LIMITER._hits.clear()


@pytest.fixture
def live_server():
    """Run the real handler on a background thread bound to an ephemeral port.

    Yields the base URL. Static files are served from the real repo root
    (BASE_DIR), so index.html/css/js reflect what's actually on disk.
    """
    port = _free_port()
    httpd = skycast_server.ThreadingHTTPServer(
        ("127.0.0.1", port), skycast_server.SkyCastHandler
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def http_get():
    """A tiny HTTP client that returns (status, headers, body) for any code.

    Plain urlopen() raises on 4xx/5xx; tests want to inspect those responses
    just as easily as a 200, so HTTPError is unwrapped into the same shape.
    """

    def _get(url: str, headers: dict | None = None):
        req = Request(url, headers=headers or {})
        try:
            with urlopen(req) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except HTTPError as err:
            return err.code, dict(err.headers.items()), err.read()

    return _get
