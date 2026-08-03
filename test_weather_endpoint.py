"""/api/weather: validation, upstream error translation, caching, rate limits."""

import json
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import server as skycast_server

SAMPLE_PAYLOAD = json.dumps(
    {
        "name": "London",
        "sys": {"country": "GB", "sunrise": 1, "sunset": 2},
        "weather": [{"main": "Clear", "description": "clear sky", "icon": "01d"}],
        "main": {"temp": 18.0, "feels_like": 17.5, "humidity": 60, "pressure": 1012},
        "wind": {"speed": 3.1},
        "visibility": 10000,
        "timezone": 0,
    }
).encode("utf-8")


def _fake_urlopen(body: bytes = SAMPLE_PAYLOAD):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    return MagicMock(return_value=cm)


def test_missing_query_params_returns_400(live_server, http_get):
    status, _, body = http_get(f"{live_server}/api/weather")
    assert status == 400
    assert "city" in json.loads(body)["error"]


def test_city_over_length_limit_returns_400(live_server, http_get):
    long_city = "x" * (skycast_server.MAX_CITY_LENGTH + 1)
    status, _, body = http_get(f"{live_server}/api/weather?city={long_city}")
    assert status == 400
    assert "characters" in json.loads(body)["error"]


def test_non_numeric_coordinates_return_400(live_server, http_get):
    status, _, body = http_get(f"{live_server}/api/weather?lat=abc&lon=def")
    assert status == 400
    assert "numbers" in json.loads(body)["error"]


def test_out_of_range_coordinates_return_400(live_server, http_get):
    status, _, body = http_get(f"{live_server}/api/weather?lat=999&lon=0")
    assert status == 400
    assert "-90" in json.loads(body)["error"]


def test_missing_api_key_returns_500(live_server, http_get, monkeypatch):
    monkeypatch.setattr(skycast_server, "API_KEY", "")
    status, _, body = http_get(f"{live_server}/api/weather?city=London")
    assert status == 500
    assert "OPENWEATHER_API_KEY" in json.loads(body)["error"]


def test_successful_lookup_then_cache_hit(live_server, http_get, monkeypatch):
    fake_urlopen = _fake_urlopen()
    monkeypatch.setattr(skycast_server, "urlopen", fake_urlopen)

    status, headers, body = http_get(f"{live_server}/api/weather?city=London")
    assert status == 200
    assert headers["X-Cache"] == "MISS"
    assert json.loads(body)["name"] == "London"
    assert fake_urlopen.call_count == 1

    # Second identical lookup should be served from cache, no new upstream call.
    status, headers, _ = http_get(f"{live_server}/api/weather?city=london")
    assert status == 200
    assert headers["X-Cache"] == "HIT"
    assert fake_urlopen.call_count == 1


def test_coords_lookup_success(live_server, http_get, monkeypatch):
    monkeypatch.setattr(skycast_server, "urlopen", _fake_urlopen())
    status, _, body = http_get(f"{live_server}/api/weather?lat=51.5&lon=-0.12")
    assert status == 200
    assert json.loads(body)["name"] == "London"


def test_response_never_contains_api_key(live_server, http_get, monkeypatch):
    monkeypatch.setattr(skycast_server, "API_KEY", "super-secret-key")
    monkeypatch.setattr(skycast_server, "urlopen", _fake_urlopen())
    _, _, body = http_get(f"{live_server}/api/weather?city=London")
    assert b"super-secret-key" not in body


def test_upstream_city_not_found_returns_404(live_server, http_get, monkeypatch):
    def raise_404(*_args, **_kwargs):
        raise HTTPError("url", 404, "Not Found", {}, None)

    monkeypatch.setattr(skycast_server, "urlopen", raise_404)
    status, _, body = http_get(f"{live_server}/api/weather?city=Nowhereville")
    assert status == 404
    assert "not found" in json.loads(body)["error"].lower()


def test_upstream_rejects_key_returns_502(live_server, http_get, monkeypatch):
    def raise_401(*_args, **_kwargs):
        raise HTTPError("url", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(skycast_server, "urlopen", raise_401)
    status, _, body = http_get(f"{live_server}/api/weather?city=London")
    assert status == 502
    assert "key" in json.loads(body)["error"].lower()


def test_upstream_network_failure_returns_502(live_server, http_get, monkeypatch):
    def raise_network_error(*_args, **_kwargs):
        raise URLError("no route to host")

    monkeypatch.setattr(skycast_server, "urlopen", raise_network_error)
    status, _, body = http_get(f"{live_server}/api/weather?city=London")
    assert status == 502
    assert "reach" in json.loads(body)["error"].lower()


def test_rate_limit_returns_429_with_retry_after(live_server, http_get, monkeypatch):
    monkeypatch.setattr(skycast_server, "urlopen", _fake_urlopen())
    monkeypatch.setattr(skycast_server.WEATHER_RATE_LIMITER, "_max", 3)

    statuses = [
        http_get(f"{live_server}/api/weather?city=City{i}")[0] for i in range(4)
    ]
    assert statuses == [200, 200, 200, 429]

    status, headers, body = http_get(f"{live_server}/api/weather?city=OneMore")
    assert status == 429
    assert int(headers["Retry-After"]) > 0
    assert "too many requests" in json.loads(body)["error"].lower()
