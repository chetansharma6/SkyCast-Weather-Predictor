"""
SkyCast — backend proxy server (Python standard library only).

Why this file exists
--------------------
The OpenWeather API key must never reach the browser or Git. A pure
front-end app cannot hide the key (it would travel in the browser's
network request). So this tiny server sits in the middle:

    browser  ->  /api/weather  ->  THIS server (adds secret key)  ->  OpenWeather

The key is read from the git-ignored .env file and is only ever used
here, server-side. The browser only ever talks to /api/... on this
server and never sees the key.

Run it with:   python server.py
Then open:     http://localhost:8000

No third-party packages required — only Python's standard library.

Performance notes
-----------------
This server is small but not naive. To stay fast it:
  * speaks HTTP/1.1 with keep-alive, so index + css + js load over a
    single reused connection instead of a handshake per file;
  * reads each static asset from disk once and caches the bytes (plus a
    gzip-compressed copy) in memory, keyed by file mtime;
  * supports conditional requests (ETag / Last-Modified), so a browser
    that already has an asset gets a tiny 304 instead of the whole file;
  * gzips compressible text when the client advertises support;
  * caches upstream weather responses briefly, so repeated lookups for
    the same place skip the network round-trip and save API quota.
"""

import gzip
import json
import os
import sys
import time
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

BASE_DIR = Path(__file__).resolve().parent
OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"

# Static assets live at the project root now (index.html + css/ + js/).
# To avoid ever serving secrets or source, we use a strict allow-list:
# only index.html at the root, and files inside these folders, are public.
STATIC_ROOT_FILES = {"index.html", "favicon.ico"}
STATIC_DIRS = {"css", "js"}

# Content types we are willing to serve, and whether each compresses well.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}
# Text-ish types worth gzipping (binary formats like png/ico don't benefit).
COMPRESSIBLE = {
    "text/html; charset=utf-8",
    "text/css; charset=utf-8",
    "text/javascript; charset=utf-8",
    "application/json; charset=utf-8",
    "image/svg+xml",
    "application/manifest+json",
}
# Only compress bodies above this size; tiny payloads aren't worth the header.
GZIP_MIN_BYTES = 256

# How long (seconds) a fetched weather payload may be reused before we go
# back to OpenWeather. Conditions don't change second-to-second, so a short
# window cuts latency and upstream calls without noticeably stale data.
WEATHER_TTL = 120


# --------------------------------------------------------------------------- #
#  Tiny .env loader (so we don't need python-dotenv)
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't clobber a value already set in the real environment.
        os.environ.setdefault(key, value)


load_env(BASE_DIR / ".env")

API_KEY = os.environ.get("OPENWEATHER_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8000"))


# --------------------------------------------------------------------------- #
#  In-memory caches (shared across threads)
# --------------------------------------------------------------------------- #
class _StaticCache:
    """Caches file bytes + a gzip copy, invalidated when the file changes."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = Lock()

    def get(self, target: Path):
        """Return an entry dict for `target`, refreshing it if the file moved.

        Entry keys: raw, gz, etag, last_modified, content_type.
        Returns None if the file no longer exists.
        """
        try:
            stat = target.stat()
        except OSError:
            return None

        key = str(target)
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._entries.get(key)
            if cached and cached["signature"] == signature:
                return cached

        # Read outside the lock — the file is small and this keeps the lock
        # from being held across disk I/O.
        raw = target.read_bytes()
        content_type = CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        gz = (
            gzip.compress(raw, compresslevel=6)
            if content_type in COMPRESSIBLE and len(raw) >= GZIP_MIN_BYTES
            else None
        )
        entry = {
            "signature": signature,
            "raw": raw,
            "gz": gz,
            "etag": '"%x-%x"' % (stat.st_size, stat.st_mtime_ns),
            "last_modified": formatdate(stat.st_mtime, usegmt=True),
            "content_type": content_type,
        }
        with self._lock:
            self._entries[key] = entry
        return entry


class _WeatherCache:
    """Short-TTL cache of upstream weather payloads keyed by the user query."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, bytes]] = {}
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            hit = self._entries.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            if hit:  # expired — drop it
                self._entries.pop(key, None)
        return None

    def put(self, key: str, body: bytes) -> None:
        with self._lock:
            self._entries[key] = (time.time() + self._ttl, body)


STATIC_CACHE = _StaticCache()
WEATHER_CACHE = _WeatherCache(WEATHER_TTL)


class SkyCastHandler(BaseHTTPRequestHandler):
    server_version = "SkyCast"
    # HTTP/1.1 enables keep-alive so the browser reuses one connection for
    # the whole page load. Safe here because every response we send carries
    # an accurate Content-Length (or is an explicit 304).
    protocol_version = "HTTP/1.1"

    # Don't advertise the runtime. BaseHTTPRequestHandler normally appends
    # "Python/3.x.y" to the Server header, which leaks implementation
    # details; return a single opaque token instead.
    def version_string(self) -> str:  # noqa: D401 (stdlib override)
        return self.server_version

    def end_headers(self) -> None:
        # Baseline hardening headers applied to every response.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    # ----- low-level body writer ------------------------------------------ #
    def _client_accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def _send_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        gz: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        """Send a full response, gzipping compressible bodies when possible.

        `gz` is a precomputed gzip copy (from the static cache); when absent
        we compress on the fly for compressible types.
        """
        use_gzip = self._client_accepts_gzip() and content_type in COMPRESSIBLE
        if use_gzip and gz is None and len(body) >= GZIP_MIN_BYTES:
            gz = gzip.compress(body, compresslevel=6)
        payload = gz if (use_gzip and gz is not None) else body
        encoded = use_gzip and gz is not None

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if content_type in COMPRESSIBLE:
            # Caches must key on encoding since we vary the body by it.
            self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    # ----- JSON helpers --------------------------------------------------- #
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_body(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers={"Cache-Control": "no-store"},
        )

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # ----- routing -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/weather":
            self.handle_weather(parse_qs(parsed.query))
        else:
            self.serve_static(path)

    # HEAD support falls out for free — reuse GET routing; _send_body skips
    # the body when the method is HEAD.
    do_HEAD = do_GET

    # ----- /api/weather --------------------------------------------------- #
    def handle_weather(self, query: dict) -> None:
        if not API_KEY:
            self._send_error_json(
                500,
                "Server is missing OPENWEATHER_API_KEY. Copy .env.example to "
                ".env and add your key, then restart the server.",
            )
            return

        # Build the upstream query from a strict allow-list of parameters.
        params = {"appid": API_KEY, "units": "metric"}

        city = (query.get("city", [""])[0]).strip()
        lat = (query.get("lat", [""])[0]).strip()
        lon = (query.get("lon", [""])[0]).strip()

        if city:
            params["q"] = city
            cache_key = f"city:{city.lower()}"
        elif lat and lon:
            params["lat"] = lat
            params["lon"] = lon
            cache_key = f"coord:{lat},{lon}"
        else:
            self._send_error_json(
                400, "Provide a 'city' or both 'lat' and 'lon' parameters."
            )
            return

        # Serve a fresh-enough cached payload without touching the network.
        cached = WEATHER_CACHE.get(cache_key)
        if cached is not None:
            self._send_body(
                200,
                cached,
                "application/json; charset=utf-8",
                extra_headers={"Cache-Control": "no-store", "X-Cache": "HIT"},
            )
            return

        url = f"{OWM_BASE}?{urlencode(params)}"

        try:
            req = Request(url, headers={"User-Agent": "SkyCast/1.0"})
            with urlopen(req, timeout=10) as resp:
                body = resp.read()
            # Validate it's JSON before caching/forwarding.
            json.loads(body.decode("utf-8"))
            WEATHER_CACHE.put(cache_key, body)
            self._send_body(
                200,
                body,
                "application/json; charset=utf-8",
                extra_headers={"Cache-Control": "no-store", "X-Cache": "MISS"},
            )

        except HTTPError as err:
            # Translate upstream errors into friendly, key-free messages.
            if err.code == 404:
                self._send_error_json(
                    404, "City not found. Check the spelling and try again."
                )
            elif err.code in (401, 403):
                self._send_error_json(
                    502,
                    "Weather service rejected the API key. Verify the key in "
                    ".env is correct and activated.",
                )
            else:
                self._send_error_json(
                    502, f"Weather service error (HTTP {err.code})."
                )

        except (URLError, TimeoutError):
            self._send_error_json(
                502,
                "Could not reach the weather service. Check your internet "
                "connection and try again.",
            )
        except Exception:  # noqa: BLE001 — last-resort guard
            self._send_error_json(500, "Unexpected server error.")

    # ----- static files -------------------------------------------------- #
    def _not_modified(self, entry: dict) -> bool:
        """True if the client's cached copy of `entry` is still current."""
        inm = self.headers.get("If-None-Match")
        if inm and entry["etag"] in [t.strip() for t in inm.split(",")]:
            return True
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                since = parsedate_to_datetime(ims)
                last = parsedate_to_datetime(entry["last_modified"])
                if since is not None and last is not None and last <= since:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    def serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"

        rel = path.lstrip("/")
        parts = rel.split("/")

        # Allow-list: only index.html (and favicon) at the root, or files
        # inside css/ and js/. This guarantees .env, server.py, .git, etc.
        # can never be served, no matter what the client requests.
        allowed = (
            (len(parts) == 1 and parts[0] in STATIC_ROOT_FILES)
            or (len(parts) > 1 and parts[0] in STATIC_DIRS)
        )
        if not allowed or any(p in ("", "..", ".") for p in parts):
            self._send_error_json(404, "Not found.")
            return

        # Resolve against BASE_DIR and refuse anything that escapes it.
        target = (BASE_DIR / rel).resolve()
        try:
            target.relative_to(BASE_DIR)
        except ValueError:
            self._send_error_json(403, "Forbidden.")
            return

        entry = STATIC_CACHE.get(target)
        if entry is None:
            self._send_error_json(404, "Not found.")
            return

        # Validators + a modest cache window. Filenames aren't content-hashed,
        # so we keep max-age short and let the ETag revalidate the rest.
        validators = {
            "ETag": entry["etag"],
            "Last-Modified": entry["last_modified"],
            "Cache-Control": "public, max-age=300",
        }

        if self._not_modified(entry):
            self.send_response(304)
            for name, value in validators.items():
                self.send_header(name, value)
            self.end_headers()
            return

        self._send_body(
            200,
            entry["raw"],
            entry["content_type"],
            gz=entry["gz"],
            extra_headers=validators,
        )

    # Quieter, tidier logging.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            f"  {self.address_string()} - {fmt % args}\n"
        )


def main() -> None:
    if not (BASE_DIR / "index.html").is_file():
        print(f"ERROR: index.html not found at {BASE_DIR}", file=sys.stderr)
        sys.exit(1)

    banner = "  SkyCast is running!"
    print("=" * 52)
    print(banner)
    print("=" * 52)
    print(f"  Open:  http://localhost:{PORT}")
    if not API_KEY:
        print("  WARNING: OPENWEATHER_API_KEY is not set.")
        print("           Copy .env.example to .env and add your key.")
    print("  Press Ctrl+C to stop.")
    print("=" * 52)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), SkyCastHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  SkyCast stopped. Bye!")
        server.server_close()


if __name__ == "__main__":
    main()
