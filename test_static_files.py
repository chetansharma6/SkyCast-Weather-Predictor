"""Static file serving: allow-list, path-traversal protection, caching."""

import gzip
import json


def test_index_served_at_root(live_server, http_get):
    status, headers, body = http_get(f"{live_server}/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<title>SkyCast" in body


def test_css_and_js_served(live_server, http_get):
    status, headers, _ = http_get(f"{live_server}/css/style.css")
    assert status == 200
    assert headers["Content-Type"] == "text/css; charset=utf-8"

    status, headers, _ = http_get(f"{live_server}/js/api.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"


def test_gzip_served_when_client_accepts_it(live_server, http_get):
    status, headers, body = http_get(
        f"{live_server}/css/style.css", headers={"Accept-Encoding": "gzip"}
    )
    assert status == 200
    assert headers.get("Content-Encoding") == "gzip"
    # The raw bytes should decompress back to real CSS.
    assert b"SkyCast" in gzip.decompress(body)


def test_etag_round_trip_returns_304(live_server, http_get):
    status, headers, _ = http_get(f"{live_server}/index.html")
    assert status == 200
    etag = headers["ETag"]

    status, _, body = http_get(
        f"{live_server}/index.html", headers={"If-None-Match": etag}
    )
    assert status == 304
    assert body == b""


def test_unknown_path_returns_404_json(live_server, http_get):
    status, headers, body = http_get(f"{live_server}/nope")
    assert status == 404
    assert json.loads(body)["error"]


def test_files_outside_allow_list_are_not_served(live_server, http_get):
    # requirements.txt and render.yaml exist on disk but are not in the
    # static allow-list, so they must 404 rather than leak file contents.
    for path in ("/requirements.txt", "/render.yaml", "/.env", "/server.py"):
        status, _, body = http_get(f"{live_server}{path}")
        assert status == 404, f"{path} should not be servable"
        assert b"OPENWEATHER" not in body


def test_path_traversal_attempts_are_blocked(live_server, http_get):
    traversal_paths = [
        "/../server.py",
        "/../../etc/passwd",
        "/css/../../server.py",
        "/css/../server.py",
        "/js/..%2f..%2fserver.py",
    ]
    for path in traversal_paths:
        status, _, body = http_get(f"{live_server}{path}")
        assert status == 404, f"{path} should not escape the static root"
        assert b"OPENWEATHER" not in body
        assert b"def main" not in body


def test_security_headers_present_on_every_response(live_server, http_get):
    for path in ("/", "/css/style.css", "/nope"):
        _, headers, _ = http_get(f"{live_server}{path}")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert "geolocation=(self)" in headers["Permissions-Policy"]
