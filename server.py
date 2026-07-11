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
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
OWM_BASE = "https://api.openweathermap.org/data/2.5/weather"


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

# Which file extensions we are willing to serve, and their content types.
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


class SkyCastHandler(BaseHTTPRequestHandler):
    server_version = "SkyCast/1.0"

    # ----- helpers -------------------------------------------------------- #
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        elif lat and lon:
            params["lat"] = lat
            params["lon"] = lon
        else:
            self._send_error_json(
                400, "Provide a 'city' or both 'lat' and 'lon' parameters."
            )
            return

        url = f"{OWM_BASE}?{urlencode(params)}"

        try:
            req = Request(url, headers={"User-Agent": "SkyCast/1.0"})
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._send_json(200, data)

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
    def serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"

        # Resolve against PUBLIC_DIR and refuse anything that escapes it.
        target = (PUBLIC_DIR / path.lstrip("/")).resolve()
        try:
            target.relative_to(PUBLIC_DIR)
        except ValueError:
            self._send_error_json(403, "Forbidden.")
            return

        if not target.is_file():
            self._send_error_json(404, "Not found.")
            return

        content_type = CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Quieter, tidier logging.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            f"  {self.address_string()} - {fmt % args}\n"
        )


def main() -> None:
    if not PUBLIC_DIR.is_dir():
        print(f"ERROR: public/ folder not found at {PUBLIC_DIR}", file=sys.stderr)
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
