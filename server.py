```python
"""
SkyCast — backend proxy server.

Browser
    -> /api/weather
    -> this server
    -> OpenWeather

The OpenWeather API key stays server-side and is never sent to the browser.

Standard library only.

Run locally:
    python server.py

Then open:
    http://localhost:8000

Environment:
    OPENWEATHER_API_KEY=your_key_here
    PORT=8000

Optional:
    HOST=0.0.0.0
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

try:
    PORT = int(os.environ.get("PORT", "8000"))
except ValueError:
    PORT = 8000

# Weather responses are reused for this many seconds.
WEATHER_TTL = 120

# Prevent unlimited memory growth from unique searches.
WEATHER_CACHE_MAX_ENTRIES = 500

# Per-client API protection.
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_SWEEP_INTERVAL = 300
RATE_LIMIT_MAX_CLIENTS = 10_000

# Input limits.
MAX_CITY_LENGTH = 100
MAX_REQUEST_TARGET_LENGTH = 2048

# Don't allow an unexpectedly large upstream response into memory.
MAX_UPSTREAM_RESPONSE_BYTES = 128 * 1024

# OpenWeather timeout.
UPSTREAM_TIMEOUT_SECONDS = 8

# Compression.
GZIP_MIN_BYTES = 256

# Browser caching for static files.
STATIC_CACHE_MAX_AGE = 300


# ============================================================================
# Static file allow-list
# ============================================================================

# Files allowed directly under the project root.
STATIC_ROOT_FILES = {
    "index.html",
    "favicon.ico",
}

# Directories whose contents may be served.
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
# Environment loader
# ============================================================================

def load_env(path: Path) -> None:
    """
    Small .env loader using only the Python standard library.

    Existing environment variables always take precedence.
    """

    if not path.is_file():
        return

    try:
        lines = path.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if not key:
            continue

        # Remove matching surrounding quotes.
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        # Don't overwrite real environment variables.
        os.environ.setdefault(
            key,
            value,
        )


load_env(
    BASE_DIR / ".env"
)

API_KEY = os.environ.get(
    "OPENWEATHER_API_KEY",
    "",
).strip()


# ============================================================================
# Utility helpers
# ============================================================================

def json_bytes(payload: dict) -> bytes:
    """
    Compact JSON encoding to reduce response size.
    """

    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def normalize_city(city: str) -> str:
    """
    Normalize whitespace and case for stable cache keys.
    """

    return " ".join(
        city.split()
    ).casefold()


def valid_latitude(value: float) -> bool:
    return -90.0 <= value <= 90.0


def valid_longitude(value: float) -> bool:
    return -180.0 <= value <= 180.0


# ============================================================================
# Static asset cache
# ============================================================================

class _StaticCache:
    """
    In-memory cache for static files.

    Cached values:
        raw
        gz
        etag
        last_modified
        content_type
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = Lock()

    def get(
        self,
        target: Path,
    ) -> dict | None:

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

            if (
                cached is not None
                and cached["signature"] == signature
            ):
                return cached

        try:
            raw = target.read_bytes()
        except OSError:
            return None

        content_type = CONTENT_TYPES.get(
            target.suffix.lower(),
            "application/octet-stream",
        )

        gz = None

        if (
            content_type in COMPRESSIBLE
            and len(raw) >= GZIP_MIN_BYTES
        ):
            gz = gzip.compress(
                raw,
                compresslevel=6,
            )

        entry = {
            "signature": signature,
            "raw": raw,
            "gz": gz,
            "etag": (
                f'"{stat.st_size:x}-'
                f'{stat.st_mtime_ns:x}"'
            ),
            "last_modified": formatdate(
                stat.st_mtime,
                usegmt=True,
            ),
            "content_type": content_type,
        }

        with self._lock:
            self._entries[key] = entry

        return entry


STATIC_CACHE = _StaticCache()


# ============================================================================
# Weather cache
# ============================================================================

class _WeatherCache:
    """
    TTL + LRU weather cache.

    The original class name is intentionally preserved for compatibility
    with the existing unit tests.

    Additional loading-state tracking prevents a cache stampede:

        request A -> OpenWeather
        request B -> waits/reuses A
        request C -> waits/reuses A

    instead of:

        request A -> OpenWeather
        request B -> OpenWeather
        request C -> OpenWeather
    """

    def __init__(
        self,
        ttl: float,
        max_entries: int,
    ) -> None:

        self._ttl = float(ttl)
        self._max_entries = int(max_entries)

        self._entries: OrderedDict[
            str,
            tuple[float, bytes],
        ] = OrderedDict()

        self._loading: set[str] = set()

        self._condition = Condition(
            Lock()
        )

    def get(
        self,
        key: str,
    ) -> bytes | None:

        now = time.monotonic()

        with self._condition:
            item = self._entries.get(key)

            if item is None:
                return None

            expires_at, body = item

            if expires_at <= now:
                self._entries.pop(
                    key,
                    None,
                )
                return None

            # Successful read makes this the most recently used entry.
            self._entries.move_to_end(
                key
            )

            return body

    def put(
        self,
        key: str,
        body: bytes,
    ) -> None:

        expires_at = (
            time.monotonic()
            + self._ttl
        )

        with self._condition:

            self._entries[key] = (
                expires_at,
                body,
            )

            self._entries.move_to_end(
                key
            )

            while (
                len(self._entries)
                > self._max_entries
            ):
                self._entries.popitem(
                    last=False
                )

            # A successful cache write means loading has finished.
            self._loading.discard(
                key
            )

            self._condition.notify_all()

    def begin_load(
        self,
        key: str,
    ) -> bool:
        """
        Mark a cache key as being fetched.

        Returns True for the request responsible for fetching it.

        Returns False for concurrent requests that should wait.
        """

        with self._condition:

            if key in self._loading:
                return False

            self._loading.add(
                key
            )

            return True

    def finish_load(
        self,
        key: str,
    ) -> None:

        with self._condition:

            self._loading.discard(
                key
            )

            self._condition.notify_all()

    def wait_for_load(
        self,
        key: str,
        timeout: float = 2.0,
    ) -> bytes | None:
        """
        Wait for another request to populate the same key.

        Returns the cached body if available.

        Returns None if the other request failed or took too long.
        """

        deadline = (
            time.monotonic()
            + timeout
        )

        with self._condition:

            while key in self._loading:

                remaining = (
                    deadline
                    - time.monotonic()
                )

                if remaining <= 0:
                    break

                self._condition.wait(
                    timeout=remaining
                )

            item = self._entries.get(
                key
            )

            if item is None:
                return None

            expires_at, body = item

            if (
                expires_at
                <= time.monotonic()
            ):
                self._entries.pop(
                    key,
                    None,
                )
                return None

            self._entries.move_to_end(
                key
            )

            return body

    # ----------------------------------------------------------------------
    # Compatibility helpers
    # ----------------------------------------------------------------------

    @property
    def _entries_public(self):
        """
        Internal compatibility accessor.

        Existing code that inspects _entries continues to work because
        _entries itself remains the actual storage attribute.
        """

        return self._entries


# ============================================================================
# Rate limiter
# ============================================================================

class _RateLimiter:
    """
    In-process sliding-window rate limiter.

    The original class name and constructor arguments are intentionally
    preserved for compatibility with the existing tests.

    This is an abuse-control layer, not a replacement for a WAF or
    reverse-proxy rate limiter.
    """

    def __init__(
        self,
        max_requests: int,
        window: float,
        sweep_interval: float,
        max_clients: int = RATE_LIMIT_MAX_CLIENTS,
    ) -> None:

        self._max = int(
            max_requests
        )

        self._window = float(
            window
        )

        self._sweep_interval = float(
            sweep_interval
        )

        self._max_clients = int(
            max_clients
        )

        self._hits: dict[
            str,
            deque[float],
        ] = {}

        self._lock = Lock()

        self._next_sweep = (
            time.monotonic()
            + self._sweep_interval
        )

    def allow(
        self,
        key: str,
    ) -> tuple[bool, float]:
        """
        Returns:

            (True, 0.0)

        when allowed.

        Otherwise:

            (False, retry_after_seconds)
        """

        now = time.monotonic()

        with self._lock:

            self._maybe_sweep(
                now
            )

            bucket = self._hits.get(
                key
            )

            if bucket is None:

                if (
                    len(self._hits)
                    >= self._max_clients
                ):
                    return (
                        False,
                        self._window,
                    )

                bucket = deque()

                self._hits[key] = bucket

            cutoff = (
                now
                - self._window
            )

            while (
                bucket
                and bucket[0] <= cutoff
            ):
                bucket.popleft()

            if (
                len(bucket)
                >= self._max
            ):

                retry_after = max(
                    bucket[0]
                    + self._window
                    - now,
                    0.0,
                )

                return (
                    False,
                    retry_after,
                )

            bucket.append(
                now
            )

            return (
                True,
                0.0,
            )

    def _maybe_sweep(
        self,
        now: float,
    ) -> None:
        """
        Remove stale client buckets.

        Caller must already hold _lock.
        """

        if now < self._next_sweep:
            return

        self._next_sweep = (
            now
            + self._sweep_interval
        )

        cutoff = (
            now
            - self._window
        )

        stale = [
            key
            for key, bucket
            in self._hits.items()
            if (
                not bucket
                or bucket[-1] <= cutoff
            )
        ]

        for key in stale:
            self._hits.pop(
                key,
                None,
            )


WEATHER_CACHE = _WeatherCache(
    ttl=WEATHER_TTL,
    max_entries=WEATHER_CACHE_MAX_ENTRIES,
)

WEATHER_RATE_LIMITER = _RateLimiter(
    max_requests=RATE_LIMIT_MAX_REQUESTS,
    window=RATE_LIMIT_WINDOW_SECONDS,
    sweep_interval=RATE_LIMIT_SWEEP_INTERVAL,
)


# ============================================================================
# HTTP handler
# ============================================================================

class SkyCastHandler(
    BaseHTTPRequestHandler
):

    protocol_version = "HTTP/1.1"

    # Don't expose Python's exact version.
    server_version = "SkyCast"
    sys_version = ""

    # ----------------------------------------------------------------------
    # Server headers
    # ----------------------------------------------------------------------

    def version_string(self) -> str:
        return self.server_version

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
            "geolocation=(self), "
            "camera=(), "
            "microphone=(), "
            "payment=()",
        )

        # The browser talks to SkyCast itself.
        # OpenWeather does not need to be in connect-src.
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

    def _client_ip(self) -> str:
        """
        Return the direct peer address.

        We deliberately do not trust X-Forwarded-For because an Internet
        client can spoof that header unless a trusted reverse proxy strips
        and replaces it.
        """

        return self.client_address[0]

    def _accepts_gzip(self) -> bool:

        return (
            "gzip"
            in self.headers.get(
                "Accept-Encoding",
                "",
            ).lower()
        )

    # ----------------------------------------------------------------------
    # Response helpers
    # ----------------------------------------------------------------------

    def _send_body(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        gz: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:

        use_gzip = (
            self._accepts_gzip()
            and content_type
            in COMPRESSIBLE
        )

        if (
            use_gzip
            and gz is None
            and len(body) >= GZIP_MIN_BYTES
        ):
            gz = gzip.compress(
                body,
                compresslevel=6,
            )

        if (
            use_gzip
            and gz is not None
        ):
            payload = gz
            encoded = True
        else:
            payload = body
            encoded = False

        self.send_response(
            status
        )

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

        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(
                    name,
                    value,
                )

        self.end_headers()

        if self.command == "HEAD":
            return

        try:
            self.wfile.write(
                payload
            )
        except (
            BrokenPipeError,
            ConnectionResetError,
        ):
            pass

    def _send_json(
        self,
        status: int,
        payload: dict,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:

        headers = {
            "Cache-Control": "no-store",
        }

        if extra_headers:
            headers.update(
                extra_headers
            )

        self._send_body(
            status,
            json_bytes(payload),
            "application/json; charset=utf-8",
            extra_headers=headers,
        )

    def _send_error_json(
        self,
        status: int,
        message: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:

        self._send_json(
            status,
            {"error": message},
            extra_headers=extra_headers,
        )

    # ----------------------------------------------------------------------
    # Routing
    # ----------------------------------------------------------------------

    def do_GET(self) -> None:

        if len(self.path) > MAX_REQUEST_TARGET_LENGTH:
            self._send_error_json(
                414,
                "Request URI is too long.",
            )
            return

        parsed = urlparse(
            self.path
        )

        if parsed.path == "/api/weather":

            query = parse_qs(
                parsed.query,
                keep_blank_values=False,
            )

            self._handle_weather(
                query
            )

            return

        self._serve_static(
            parsed.path
        )

    # HEAD uses the same routing and response metadata.
    do_HEAD = do_GET

    # ----------------------------------------------------------------------
    # Weather endpoint
    # ----------------------------------------------------------------------

    def _handle_weather(
        self,
        query: dict[str, list[str]],
    ) -> None:

        allowed, retry_after = (
            WEATHER_RATE_LIMITER.allow(
                self._client_ip()
            )
        )

        if not allowed:

            self._send_error_json(
                429,
                "Too many requests. Please wait a moment and try again.",
                extra_headers={
                    "Retry-After": str(
                        max(
                            1,
                            int(retry_after) + 1,
                        )
                    )
                },
            )

            return

        if not API_KEY:

            # Never expose whether/how an upstream credential is stored.
            self._send_error_json(
                500,
                "Weather service is not configured.",
            )

            return

        city = (
            query.get(
                "city",
                [""],
            )[0]
            .strip()
        )

        lat_raw = (
            query.get(
                "lat",
                [""],
            )[0]
            .strip()
        )

        lon_raw = (
            query.get(
                "lon",
                [""],
            )[0]
            .strip()
        )

        # ------------------------------------------------------------------
        # City request
        # ------------------------------------------------------------------

        if city:

            if len(city) > MAX_CITY_LENGTH:

                self._send_error_json(
                    400,
                    (
                        "City name must be "
                        f"{MAX_CITY_LENGTH} characters or fewer."
                    ),
                )

                return

            # Collapse repeated whitespace.
            city = " ".join(
                city.split()
            )

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
        # Coordinate request
        # ------------------------------------------------------------------

        elif lat_raw and lon_raw:

            try:
                lat = float(
                    lat_raw
                )
                lon = float(
                    lon_raw
                )

            except (
                ValueError,
                TypeError,
            ):

                self._send_error_json(
                    400,
                    "Latitude and longitude must be valid numbers.",
                )

                return

            # Reject NaN and infinity as well as normal out-of-range values.
            if (
                not valid_latitude(lat)
                or not valid_longitude(lon)
            ):

                self._send_error_json(
                    400,
                    "Coordinates are outside the valid range.",
                )

                return

            # Four decimal places is plenty for weather lookup and prevents
            # endless cache fragmentation through tiny coordinate changes.
            lat = round(
                lat,
                4,
            )

            lon = round(
                lon,
                4,
            )

            cache_key = (
                f"coord:{lat:.4f},"
                f"{lon:.4f}"
            )

            upstream_params = {
                "lat": f"{lat:.4f}",
                "lon": f"{lon:.4f}",
                "appid": API_KEY,
                "units": "metric",
            }

        # ------------------------------------------------------------------
        # Missing search parameter
        # ------------------------------------------------------------------

        else:

            self._send_error_json(
                400,
                (
                    "Provide a city or both "
                    "latitude and longitude."
                ),
            )

            return

        # ------------------------------------------------------------------
        # Fast cache hit
        # ------------------------------------------------------------------

        cached = WEATHER_CACHE.get(
            cache_key
        )

        if cached is not None:

            self._send_body(
                200,
                cached,
                "application/json; charset=utf-8",
                extra_headers={
                    "Cache-Control": "no-store",
                    "X-Cache": "HIT",
                },
            )

            return

        # ------------------------------------------------------------------
        # Prevent duplicate upstream requests
        # ------------------------------------------------------------------

        if not WEATHER_CACHE.begin_load(
            cache_key
        ):

            # Another request is already obtaining this weather result.
            cached = WEATHER_CACHE.wait_for_load(
                cache_key,
                timeout=2.0,
            )

            if cached is not None:

                self._send_body(
                    200,
                    cached,
                    "application/json; charset=utf-8",
                    extra_headers={
                        "Cache-Control": "no-store",
                        "X-Cache": "COALESCED",
                    },
                )

                return

            self._send_error_json(
                503,
                "Weather service is busy. Please try again.",
                extra_headers={
                    "Retry-After": "2",
                },
            )

            return

        try:

            self._fetch_weather(
                cache_key,
                upstream_params,
            )

        finally:

            # This is critical: a failed request must never permanently
            # leave the key in the "loading" state.
            WEATHER_CACHE.finish_load(
                cache_key
            )

    # ----------------------------------------------------------------------
    # OpenWeather request
    # ----------------------------------------------------------------------

    def _fetch_weather(
        self,
        cache_key: str,
        params: dict[str, str],
    ) -> None:

        url = (
            OWM_BASE
            + "?"
            + urlencode(params)
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

                declared_length = response.headers.get(
                    "Content-Length"
                )

                if declared_length:

                    try:
                        if (
                            int(declared_length)
                            > MAX_UPSTREAM_RESPONSE_BYTES
                        ):
                            self._send_error_json(
                                502,
                                "Weather service response is too large.",
                            )
                            return

                    except ValueError:
                        # Ignore malformed Content-Length and enforce the
                        # limit while reading.
                        pass

                chunks: list[bytes] = []
                total = 0

                while True:

                    chunk = response.read(
                        8192
                    )

                    if not chunk:
                        break

                    total += len(
                        chunk
                    )

                    if (
                        total
                        > MAX_UPSTREAM_RESPONSE_BYTES
                    ):

                        self._send_error_json(
                            502,
                            "Weather service response is too large.",
                        )

                        return

                    chunks.append(
                        chunk
                    )

                body = b"".join(
                    chunks
                )

            # Never cache arbitrary upstream content.
            try:

                json.loads(
                    body.decode(
                        "utf-8"
                    )
                )

            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):

                self._send_error_json(
                    502,
                    "Weather service returned invalid data.",
                )

                return

            WEATHER_CACHE.put(
                cache_key,
                body,
            )

            self._send_body(
                200,
                body,
                "application/json; charset=utf-8",
                extra_headers={
                    "Cache-Control": "no-store",
                    "X-Cache": "MISS",
                },
            )

        except HTTPError as exc:

            if exc.code == 404:

                self._send_error_json(
                    404,
                    "City not found. Check the spelling and try again.",
                )

                return

            if exc.code in (
                401,
                403,
            ):

                self._send_error_json(
                    502,
                    "Weather service authentication failed.",
                )

                return

            # Don't forward upstream response bodies, URLs, or internal
            # diagnostic information to the browser.
            self._send_error_json(
                502,
                "Weather service is temporarily unavailable.",
            )

        except (
            URLError,
            TimeoutError,
            ConnectionError,
        ):

            self._send_error_json(
                502,
                "Could not reach the weather service.",
            )

        except (
            BrokenPipeError,
            ConnectionResetError,
        ):
            # Browser/client disconnected before receiving the response.
            pass

        except Exception:

            # Never expose a traceback or implementation detail.
            self._send_error_json(
                500,
                "Unexpected server error.",
            )

    # ----------------------------------------------------------------------
    # Static file serving
    # ----------------------------------------------------------------------

    def _static_path_allowed(
        self,
        relative: str,
    ) -> bool:

        parts = relative.split(
            "/"
        )

        # Reject traversal and empty path components.
        if any(
            part in (
                "",
                ".",
                "..",
            )
            for part in parts
        ):
            return False

        # Root allow-list.
        if (
            len(parts) == 1
            and parts[0]
            in STATIC_ROOT_FILES
        ):
            return True

        # Directory allow-list.
        if (
            len(parts) >= 2
            and parts[0]
            in STATIC_DIRS
        ):
            return True

        return False

    def _serve_static(
        self,
        path: str,
    ) -> None:

        if path == "/":
            path = "/index.html"

        relative = path.lstrip(
            "/"
        )

        if not self._static_path_allowed(
            relative
        ):

            self._send_error_json(
                404,
                "Not found.",
            )

            return

        target = (
            BASE_DIR / relative
        ).resolve()

        # Defense in depth against symlinks and traversal.
        try:

            target.relative_to(
                BASE_DIR
            )

        except ValueError:

            self._send_error_json(
                403,
                "Forbidden.",
            )

            return

        entry = STATIC_CACHE.get(
            target
        )

        if entry is None:

            self._send_error_json(
                404,
                "Not found.",
            )

            return

        cache_headers = {
            "ETag": entry["etag"],
            "Last-Modified": entry["last_modified"],
            "Cache-Control": (
                "public, "
                f"max-age={STATIC_CACHE_MAX_AGE}"
            ),
        }

        # Conditional request.
        if self._not_modified(
            entry
        ):

            self.send_response(
                304
            )

            for name, value in cache_headers.items():
                self.send_header(
                    name,
                    value,
                )

            self.end_headers()

            return

        self._send_body(
            200,
            entry["raw"],
            entry["content_type"],
            gz=entry["gz"],
            extra_headers=cache_headers,
        )

    # ----------------------------------------------------------------------
    # Conditional requests
    # ----------------------------------------------------------------------

    def _not_modified(
        self,
        entry: dict,
    ) -> bool:

        client_etag = self.headers.get(
            "If-None-Match"
        )

        if client_etag:

            etags = {
                value.strip()
                for value
                in client_etag.split(",")
            }

            if entry["etag"] in etags:
                return True

        modified_since = self.headers.get(
            "If-Modified-Since"
        )

        if modified_since:

            try:

                client_time = (
                    parsedate_to_datetime(
                        modified_since
                    )
                )

                server_time = (
                    parsedate_to_datetime(
                        entry["last_modified"]
                    )
                )

                if (
                    client_time is not None
                    and server_time is not None
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

        try:
            message = fmt % args
        except Exception:
            message = fmt

        sys.stderr.write(
            f"{self.client_address[0]} - "
            f"{message}\n"
        )


# ============================================================================
# Application startup
# ============================================================================

def main() -> None:

    index_file = (
        BASE_DIR / "index.html"
    )

    if not index_file.is_file():

        print(
            "ERROR: index.html not found at "
            f"{BASE_DIR}",
            file=sys.stderr,
        )

        raise SystemExit(1)

    print(
        "=" * 52
    )

    print(
        "SkyCast server"
    )

    print(
        "=" * 52
    )

    print(
        f"Listening on http://localhost:{PORT}"
    )

    if API_KEY:

        print(
            "OpenWeather API key: configured"
        )

    else:

        print(
            "WARNING: OPENWEATHER_API_KEY is not configured.",
            file=sys.stderr,
        )

    print(
        "Press Ctrl+C to stop."
    )

    print(
        "=" * 52
    )

    server = ThreadingHTTPServer(
        (
            HOST,
            PORT,
        ),
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
