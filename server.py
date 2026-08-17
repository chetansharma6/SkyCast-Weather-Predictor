```python
"""
SkyCast backend proxy.

Browser
    -> /api/weather
    -> this server
    -> OpenWeather

The OpenWeather API key stays server-side in OPENWEATHER_API_KEY.

Standard library only.

Recommended production deployment:
    Internet
        |
      HTTPS
        |
   reverse proxy / WAF
        |
    SkyCast server

This file is intentionally small and dependency-free. It is suitable for
small deployments and local use. For high-traffic production workloads,
place it behind a real reverse proxy/application server.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from collections import OrderedDict, deque
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Lock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


# ============================================================================
# Configuration
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent

OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

WEATHER_TTL = 120.0

WEATHER_CACHE_MAX_ENTRIES = 500

RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_CLIENTS = 10_000
RATE_LIMIT_SWEEP_INTERVAL = 300.0

MAX_CITY_LENGTH = 100

MAX_QUERY_LENGTH = 512

MAX_UPSTREAM_RESPONSE_BYTES = 128 * 1024

UPSTREAM_TIMEOUT_SECONDS = 8

GZIP_MIN_BYTES = 256

STATIC_CACHE_SECONDS = 300


# ============================================================================
# Static file policy
# ============================================================================

STATIC_ROOT_FILES = {
    "index.html",
    "favicon.ico",
}

STATIC_DIRS = {
    "css",
    "js",
}

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

COMPRESSIBLE = {
    "text/html; charset=utf-8",
    "text/css; charset=utf-8",
    "text/javascript; charset=utf-8",
    "application/json; charset=utf-8",
    "image/svg+xml",
    "application/manifest+json",
}


# ============================================================================
# Environment
# ============================================================================

def load_env(path: Path) -> None:
    """
    Minimal .env loader.

    Existing real environment variables always win.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"Warning: unable to read {path}: {exc}", file=sys.stderr)
        return

    for raw in text.splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


load_env(BASE_DIR / ".env")

API_KEY = os.environ.get("OPENWEATHER_API_KEY", "").strip()


# ============================================================================
# Helpers
# ============================================================================

def json_bytes(payload: dict) -> bytes:
    """
    Compact JSON encoding.

    separators remove unnecessary whitespace and reduce response size.
    """

    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def clamp_text(value: str, maximum: int) -> str:
    return value.strip()[:maximum]


def normalize_city(city: str) -> str:
    """
    Normalize city input for cache consistency.

    Multiple spaces and case differences should map to the same cache key.
    """

    return " ".join(city.split()).casefold()


def valid_coordinate(value: float, minimum: float, maximum: float) -> bool:
    return minimum <= value <= maximum


# ============================================================================
# Static cache
# ============================================================================

class StaticCache:
    """
    Thread-safe in-memory cache of static assets.

    Stores:
        raw bytes
        optional gzip bytes
        ETag
        Last-Modified
        content type
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = Lock()

    def get(self, target: Path) -> dict | None:
        try:
            stat = target.stat()
        except OSError:
            return None

        signature = (
            stat.st_mtime_ns,
            stat.st_size,
        )

        key = str(target)

        with self._lock:
            cached = self._entries.get(key)

            if cached and cached["signature"] == signature:
                return cached

        try:
            raw = target.read_bytes()
        except OSError:
            return None

        content_type = CONTENT_TYPES.get(
            target.suffix.lower(),
            "application/octet-stream",
        )

        compressed = None

        if (
            content_type in COMPRESSIBLE
            and len(raw) >= GZIP_MIN_BYTES
        ):
            compressed = gzip.compress(
                raw,
                compresslevel=6,
            )

        entry = {
            "signature": signature,
            "raw": raw,
            "gz": compressed,
            "etag": f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"',
            "last_modified": formatdate(
                stat.st_mtime,
                usegmt=True,
            ),
            "content_type": content_type,
        }

        with self._lock:
            self._entries[key] = entry

        return entry


STATIC_CACHE = StaticCache()


# ============================================================================
# Weather cache
# ============================================================================

class WeatherCache:
    """
    Size-bounded LRU cache with TTL.

    Also prevents a cache stampede by allowing only one thread to populate
    a given key at a time.
    """

    def __init__(
        self,
        ttl: float,
        max_entries: int,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries

        self.entries: OrderedDict[
            str,
            tuple[float, bytes],
        ] = OrderedDict()

        self.loading: set[str] = set()

        self.condition = Condition(Lock())

    def get(self, key: str) -> bytes | None:
        now = time.monotonic()

        with self.condition:
            item = self.entries.get(key)

            if item is None:
                return None

            expires_at, body = item

            if expires_at <= now:
                self.entries.pop(key, None)
                return None

            self.entries.move_to_end(key)

            return body

    def put(self, key: str, body: bytes) -> None:
        expires_at = time.monotonic() + self.ttl

        with self.condition:
            self.entries[key] = (
                expires_at,
                body,
            )

            self.entries.move_to_end(key)

            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)

            self.loading.discard(key)

            self.condition.notify_all()

    def begin_load(self, key: str) -> bool:
        """
        Returns True if the caller should fetch from OpenWeather.

        Returns False if another thread is already loading the same key.
        """

        with self.condition:
            if key in self.loading:
                return False

            self.loading.add(key)
            return True

    def finish_load(self, key: str) -> None:
        with self.condition:
            self.loading.discard(key)
            self.condition.notify_all()

    def wait_for_load(self, key: str, timeout: float = 2.0) -> bytes | None:
        """
        Wait briefly for another request to populate the same cache key.
        """

        deadline = time.monotonic() + timeout

        with self.condition:
            while key in self.loading:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    break

                self.condition.wait(timeout=remaining)

            item = self.entries.get(key)

            if item is None:
                return None

            expires_at, body = item

            if expires_at <= time.monotonic():
                self.entries.pop(key, None)
                return None

            self.entries.move_to_end(key)

            return body


WEATHER_CACHE = WeatherCache(
    WEATHER_TTL,
    WEATHER_CACHE_MAX_ENTRIES,
)


# ============================================================================
# Rate limiter
# ============================================================================

class RateLimiter:
    """
    Small in-process sliding-window limiter.

    Important:
        This is abuse protection, not a replacement for a WAF/reverse proxy.

    We intentionally do NOT trust X-Forwarded-For here by default.
    If you deploy behind a trusted proxy, configure that proxy to pass a
    trustworthy client identifier and adapt this function accordingly.
    """

    def __init__(
        self,
        maximum: int,
        window: float,
        max_clients: int,
        sweep_interval: float,
    ) -> None:
        self.maximum = maximum
        self.window = window
        self.max_clients = max_clients
        self.sweep_interval = sweep_interval

        self.hits: dict[str, deque[float]] = {}

        self.lock = Lock()

        self.next_sweep = (
            time.monotonic() + sweep_interval
        )

    def allow(self, client: str) -> tuple[bool, float]:
        now = time.monotonic()

        with self.lock:
            if len(self.hits) >= self.max_clients:
                self._sweep(now)

                if len(self.hits) >= self.max_clients:
                    return False, self.window

            bucket = self.hits.setdefault(
                client,
                deque(),
            )

            cutoff = now - self.window

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.maximum:
                retry_after = max(
                    bucket[0] + self.window - now,
                    0.0,
                )

                self._sweep(now)

                return False, retry_after

            bucket.append(now)

            self._sweep(now)

            return True, 0.0

    def _sweep(self, now: float) -> None:
        if now < self.next_sweep:
            return

        self.next_sweep = now + self.sweep_interval

        cutoff = now - self.window

        stale = [
            key
            for key, bucket in self.hits.items()
            if not bucket or bucket[-1] <= cutoff
        ]

        for key in stale:
            self.hits.pop(key, None)


RATE_LIMITER = RateLimiter(
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    RATE_LIMIT_MAX_CLIENTS,
    RATE_LIMIT_SWEEP_INTERVAL,
)


# ============================================================================
# HTTP handler
# ============================================================================

class SkyCastHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    server_version = "SkyCast"

    # ----------------------------------------------------------------------
    # Server information
    # ----------------------------------------------------------------------

    def version_string(self) -> str:
        return self.server_version

    # ----------------------------------------------------------------------
    # Security headers
    # ----------------------------------------------------------------------

    def end_headers(self) -> None:

        self.send_header(
            "X-Content-Type-Options",
            "nosniff",
        )

        self.send_header(
            "X-Frame-Options",
            "DENY",
        )

        self.send_header(
            "Referrer-Policy",
            "no-referrer",
        )

        self.send_header(
            "Permissions-Policy",
            "geolocation=(self), camera=(), microphone=(), payment=()",
        )

        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data: https://openweathermap.org; "
            "style-src 'self'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )

        super().end_headers()

    # ----------------------------------------------------------------------
    # Request helpers
    # ----------------------------------------------------------------------

    def client_identifier(self) -> str:
        """
        Use the actual TCP peer by default.

        Do not blindly trust X-Forwarded-For because clients can spoof it
        when the application is directly reachable.
        """

        return self.client_address[0]

    def accepts_gzip(self) -> bool:
        encoding = self.headers.get(
            "Accept-Encoding",
            "",
        )

        return "gzip" in encoding.lower()

    # ----------------------------------------------------------------------
    # Response helpers
    # ----------------------------------------------------------------------

    def send_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        gzip_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:

        use_gzip = (
            self.accepts_gzip()
            and content_type in COMPRESSIBLE
        )

        if use_gzip and gzip_body is None and len(body) >= GZIP_MIN_BYTES:
            gzip_body = gzip.compress(
                body,
                compresslevel=6,
            )

        payload = (
            gzip_body
            if use_gzip and gzip_body is not None
            else body
        )

        encoded = (
            use_gzip
            and gzip_body is not None
        )

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type,
        )

        self.send_header(
            "Content-Length",
            str(len(payload)),
        )

        if content_type in COMPRESSIBLE:
            self.send_header(
                "Vary",
                "Accept-Encoding",
            )

        if encoded:
            self.send_header(
                "Content-Encoding",
                "gzip",
            )

        if headers:
            for name, value in headers.items():
                self.send_header(
                    name,
                    value,
                )

        self.end_headers()

        if self.command != "HEAD":
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Client disappeared while receiving the response.
                pass

    def send_json(
        self,
        status: int,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:

        response_headers = {
            "Cache-Control": "no-store",
        }

        if headers:
            response_headers.update(headers)

        self.send_body(
            status,
            json_bytes(payload),
            "application/json; charset=utf-8",
            headers=response_headers,
        )

    def send_error_json(
        self,
        status: int,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:

        self.send_json(
            status,
            {"error": message},
            headers=headers,
        )

    # ----------------------------------------------------------------------
    # Routing
    # ----------------------------------------------------------------------

    def do_GET(self) -> None:

        parsed = urlparse(self.path)

        if len(self.path) > MAX_QUERY_LENGTH + 128:
            self.send_error_json(
                414,
                "Request URI is too long.",
            )
            return

        if parsed.path == "/api/weather":
            self.handle_weather(
                parse_qs(
                    parsed.query,
                    keep_blank_values=False,
                    strict_parsing=False,
                )
            )
            return

        self.serve_static(parsed.path)

    do_HEAD = do_GET

    # ----------------------------------------------------------------------
    # Weather API
    # ----------------------------------------------------------------------

    def handle_weather(self, query: dict[str, list[str]]) -> None:

        allowed, retry_after = RATE_LIMITER.allow(
            self.client_identifier()
        )

        if not allowed:
            self.send_error_json(
                429,
                "Too many requests. Please wait a moment and try again.",
                headers={
                    "Retry-After": str(
                        max(1, int(retry_after) + 1)
                    )
                },
            )
            return

        if not API_KEY:
            self.send_error_json(
                500,
                "Weather service is not configured.",
            )
            return

        city = query.get("city", [""])[0].strip()

        lat_raw = query.get(
            "lat",
            [""],
        )[0].strip()

        lon_raw = query.get(
            "lon",
            [""],
        )[0].strip()

        # ------------------------------------------------------------------
        # City lookup
        # ------------------------------------------------------------------

        if city:

            if len(city) > MAX_CITY_LENGTH:
                self.send_error_json(
                    400,
                    f"City name must be {MAX_CITY_LENGTH} characters or fewer.",
                )
                return

            city = " ".join(city.split())

            cache_key = (
                "city:"
                + normalize_city(city)
            )

            upstream_params = {
                "q": city,
                "appid": API_KEY,
                "units": "metric",
            }

        # ------------------------------------------------------------------
        # Coordinate lookup
        # ------------------------------------------------------------------

        elif lat_raw and lon_raw:

            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
            except (ValueError, TypeError):
                self.send_error_json(
                    400,
                    "Latitude and longitude must be valid numbers.",
                )
                return

            if not (
                valid_coordinate(lat, -90.0, 90.0)
                and valid_coordinate(lon, -180.0, 180.0)
            ):
                self.send_error_json(
                    400,
                    "Coordinates are outside the valid range.",
                )
                return

            # Normalize floating-point representations so:
            # 28.5 and 028.5000 become the same cache key.
            lat = round(lat, 4)
            lon = round(lon, 4)

            cache_key = f"coord:{lat:.4f},{lon:.4f}"

            upstream_params = {
                "lat": f"{lat:.4f}",
                "lon": f"{lon:.4f}",
                "appid": API_KEY,
                "units": "metric",
            }

        else:

            self.send_error_json(
                400,
                "Provide either a city or both latitude and longitude.",
            )
            return

        # ------------------------------------------------------------------
        # Cache
        # ------------------------------------------------------------------

        cached = WEATHER_CACHE.get(cache_key)

        if cached is not None:
            self.send_body(
                200,
                cached,
                "application/json; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Cache": "HIT",
                },
            )
            return

        # ------------------------------------------------------------------
        # Prevent cache stampede
        # ------------------------------------------------------------------

        should_load = WEATHER_CACHE.begin_load(
            cache_key
        )

        if not should_load:

            cached = WEATHER_CACHE.wait_for_load(
                cache_key
            )

            if cached is not None:
                self.send_body(
                    200,
                    cached,
                    "application/json; charset=utf-8",
                    headers={
                        "Cache-Control": "no-store",
                        "X-Cache": "COALESCED",
                    },
                )
                return

            # Another request is taking too long.
            self.send_error_json(
                503,
                "Weather service is busy. Please try again.",
                headers={
                    "Retry-After": "2",
                },
            )
            return

        try:
            self.fetch_weather(
                cache_key,
                upstream_params,
            )
        finally:
            WEATHER_CACHE.finish_load(
                cache_key
            )

    def fetch_weather(
        self,
        cache_key: str,
        params: dict[str, str],
    ) -> None:

        url = (
            f"{OWM_BASE}?"
            f"{urlencode(params)}"
        )

        request = Request(
            url,
            headers={
                "User-Agent": "SkyCast/1.0",
                "Accept": "application/json",
            },
        )

        try:

            with urlopen(
                request,
                timeout=UPSTREAM_TIMEOUT_SECONDS,
            ) as response:

                content_length = response.headers.get(
                    "Content-Length"
                )

                if content_length:
                    try:
                        declared_size = int(
                            content_length
                        )
                    except ValueError:
                        declared_size = 0

                    if declared_size > MAX_UPSTREAM_RESPONSE_BYTES:
                        self.send_error_json(
                            502,
                            "Weather service returned an unexpectedly large response.",
                        )
                        return

                chunks: list[bytes] = []
                total = 0

                while True:

                    chunk = response.read(8192)

                    if not chunk:
                        break

                    total += len(chunk)

                    if total > MAX_UPSTREAM_RESPONSE_BYTES:
                        self.send_error_json(
                            502,
                            "Weather service response was too large.",
                        )
                        return

                    chunks.append(chunk)

                body = b"".join(chunks)

            # Verify JSON before putting anything into the cache.
            try:
                json.loads(
                    body.decode("utf-8")
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):
                self.send_error_json(
                    502,
                    "Weather service returned invalid data.",
                )
                return

            WEATHER_CACHE.put(
                cache_key,
                body,
            )

            self.send_body(
                200,
                body,
                "application/json; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Cache": "MISS",
                },
            )

        except HTTPError as exc:

            if exc.code == 404:
                self.send_error_json(
                    404,
                    "City not found. Check the spelling and try again.",
                )
                return

            if exc.code in (401, 403):
                self.send_error_json(
                    502,
                    "Weather service authentication failed.",
                )
                return

            if 400 <= exc.code < 500:
                self.send_error_json(
                    502,
                    "Weather service rejected the request.",
                )
                return

            self.send_error_json(
                502,
                "Weather service is temporarily unavailable.",
            )

        except (
            URLError,
            TimeoutError,
            ConnectionError,
        ):
            self.send_error_json(
                502,
                "Could not reach the weather service.",
            )

        except (
            BrokenPipeError,
            ConnectionResetError,
        ):
            pass

        except Exception:
            # Never expose stack traces or implementation details.
            self.send_error_json(
                500,
                "Unexpected server error.",
            )

    # ----------------------------------------------------------------------
    # Static files
    # ----------------------------------------------------------------------

    def is_allowed_static_path(
        self,
        relative: str,
    ) -> bool:

        parts = relative.split("/")

        if any(
            part in {
                "",
                ".",
                "..",
            }
            for part in parts
        ):
            return False

        if (
            len(parts) == 1
            and parts[0] in STATIC_ROOT_FILES
        ):
            return True

        if (
            len(parts) >= 2
            and parts[0] in STATIC_DIRS
        ):
            return True

        return False

    def serve_static(
        self,
        path: str,
    ) -> None:

        if path == "/":
            path = "/index.html"

        relative = path.lstrip("/")

        if not self.is_allowed_static_path(
            relative
        ):
            self.send_error_json(
                404,
                "Not found.",
            )
            return

        target = (
            BASE_DIR / relative
        ).resolve()

        # Defense in depth against path traversal/symlink escapes.
        try:
            target.relative_to(
                BASE_DIR
            )
        except ValueError:
            self.send_error_json(
                403,
                "Forbidden.",
            )
            return

        entry = STATIC_CACHE.get(
            target
        )

        if entry is None:
            self.send_error_json(
                404,
                "Not found.",
            )
            return

        headers = {
            "ETag": entry["etag"],
            "Last-Modified": entry["last_modified"],
            "Cache-Control": (
                f"public, max-age={STATIC_CACHE_SECONDS}"
            ),
        }

        if self.not_modified(
            entry
        ):
            self.send_response(
                304
            )

            for name, value in headers.items():
                self.send_header(
                    name,
                    value,
                )

            self.end_headers()
            return

        self.send_body(
            200,
            entry["raw"],
            entry["content_type"],
            gzip_body=entry["gz"],
            headers=headers,
        )

    def not_modified(
        self,
        entry: dict,
    ) -> bool:

        etag = self.headers.get(
            "If-None-Match"
        )

        if etag:
            candidates = {
                item.strip()
                for item in etag.split(",")
            }

            if entry["etag"] in candidates:
                return True

        modified = self.headers.get(
            "If-Modified-Since"
        )

        if modified:

            try:
                client_time = parsedate_to_datetime(
                    modified
                )

                server_time = parsedate_to_datetime(
                    entry["last_modified"]
                )

                if (
                    client_time
                    and server_time
                    and server_time <= client_time
                ):
                    return True

            except (
                TypeError,
                ValueError,
            ):
                pass

        return False

    # ----------------------------------------------------------------------
    # Logging
    # ----------------------------------------------------------------------

    def log_message(
        self,
        fmt: str,
        *args,
    ) -> None:

        sys.stderr.write(
            f"{self.client_address[0]} "
            f"- {fmt % args}\n"
        )


# ============================================================================
# Startup
# ============================================================================

def main() -> None:

    if not (
        BASE_DIR / "index.html"
    ).is_file():

        print(
            "ERROR: index.html was not found at "
            f"{BASE_DIR}",
            file=sys.stderr,
        )

        raise SystemExit(1)

    print("=" * 56)
    print(" SkyCast server")
    print("=" * 56)
    print(
        f" URL: http://localhost:{PORT}"
    )

    if API_KEY:
        print(
            " OpenWeather API key: configured"
        )
    else:
        print(
            " WARNING: OpenWeather API key is not configured."
        )
        print(
            " Set OPENWEATHER_API_KEY in the environment or .env."
        )

    print(
        " Press Ctrl+C to stop."
    )
    print("=" * 56)

    server = ThreadingHTTPServer(
        (HOST, PORT),
        SkyCastHandler,
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print(
            "\nSkyCast stopped."
        )

    finally:
        server.server_close()


if __name__ == "__main__":
    main()
```
