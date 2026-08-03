"""Unit tests for the in-memory cache and rate limiter, independent of HTTP."""

import server as skycast_server


class _FakeClock:
    """Deterministic stand-in for time.time() so tests don't need real sleeps."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_weather_cache_hit_within_ttl(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    cache = skycast_server._WeatherCache(ttl=60, max_entries=10)

    cache.put("city:paris", b"payload")
    assert cache.get("city:paris") == b"payload"

    clock.advance(59)
    assert cache.get("city:paris") == b"payload"


def test_weather_cache_expires_after_ttl(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    cache = skycast_server._WeatherCache(ttl=60, max_entries=10)

    cache.put("city:paris", b"payload")
    clock.advance(61)
    assert cache.get("city:paris") is None


def test_weather_cache_evicts_oldest_when_over_capacity(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    cache = skycast_server._WeatherCache(ttl=3600, max_entries=2)

    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.put("c", b"3")  # should evict "a", the oldest

    assert cache.get("a") is None
    assert cache.get("b") == b"2"
    assert cache.get("c") == b"3"


def test_weather_cache_get_refreshes_recency(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    cache = skycast_server._WeatherCache(ttl=3600, max_entries=2)

    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.get("a")  # touch "a" so "b" becomes the oldest
    cache.put("c", b"3")  # should evict "b", not "a"

    assert cache.get("a") == b"1"
    assert cache.get("b") is None


def test_rate_limiter_allows_up_to_max(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    limiter = skycast_server._RateLimiter(max_requests=3, window=60, sweep_interval=300)

    results = [limiter.allow("1.2.3.4")[0] for _ in range(3)]
    assert results == [True, True, True]

    allowed, retry_after = limiter.allow("1.2.3.4")
    assert allowed is False
    assert retry_after > 0


def test_rate_limiter_tracks_clients_independently(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    limiter = skycast_server._RateLimiter(max_requests=1, window=60, sweep_interval=300)

    assert limiter.allow("client-a")[0] is True
    assert limiter.allow("client-a")[0] is False
    assert limiter.allow("client-b")[0] is True


def test_rate_limiter_recovers_after_window_elapses(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    limiter = skycast_server._RateLimiter(max_requests=1, window=60, sweep_interval=300)

    assert limiter.allow("client-a")[0] is True
    assert limiter.allow("client-a")[0] is False

    clock.advance(61)
    assert limiter.allow("client-a")[0] is True


def test_rate_limiter_sweep_drops_stale_buckets(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(skycast_server.time, "time", clock)
    limiter = skycast_server._RateLimiter(max_requests=5, window=10, sweep_interval=30)

    limiter.allow("stale-client")
    assert "stale-client" in limiter._hits

    clock.advance(31)  # past both the window and the sweep interval
    limiter.allow("other-client")  # any call can trigger the sweep

    assert "stale-client" not in limiter._hits
