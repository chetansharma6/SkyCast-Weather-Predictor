```python
"""
Unit tests for SkyCast's in-memory cache and rate limiter.

These tests intentionally avoid HTTP/network operations.

Coverage includes:

    - Weather cache hits
    - TTL expiration
    - LRU eviction
    - LRU recency refresh
    - Cache stampede protection
    - Loading-state cleanup
    - Rate limiting
    - Independent clients
    - Sliding-window recovery
    - Stale bucket cleanup
    - Client bucket limits

Run with:

    pytest -q
"""

import time

import server as skycast_server


# ============================================================================
# Deterministic monotonic clock
# ============================================================================

class _FakeMonotonicClock:
    """
    Deterministic replacement for time.monotonic().

    Unlike wall-clock time, monotonic time is appropriate for:
        - TTLs
        - timeouts
        - rate-limit windows

    No test should need to actually sleep.
    """

    def __init__(
        self,
        start: float = 1_000_000.0,
    ) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(
        self,
        seconds: float,
    ) -> None:
        if seconds < 0:
            raise ValueError(
                "Fake clock cannot move backwards."
            )

        self.now += seconds


# ============================================================================
# Weather cache
# ============================================================================

def make_weather_cache(
    monkeypatch,
    *,
    start: float = 1_000_000.0,
    ttl: float = 60.0,
    max_entries: int = 10,
):
    """
    Create a weather cache with deterministic time.
    """

    clock = _FakeMonotonicClock(
        start=start
    )

    monkeypatch.setattr(
        skycast_server.time,
        "monotonic",
        clock,
    )

    cache = skycast_server.WeatherCache(
        ttl=ttl,
        max_entries=max_entries,
    )

    return cache, clock


def test_weather_cache_hit_within_ttl(monkeypatch):
    cache, clock = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "city:paris",
        b"payload",
    )

    assert cache.get(
        "city:paris"
    ) == b"payload"

    clock.advance(59)

    assert cache.get(
        "city:paris"
    ) == b"payload"


def test_weather_cache_expires_at_ttl(monkeypatch):
    cache, clock = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "city:paris",
        b"payload",
    )

    clock.advance(60)

    # At the exact expiration boundary the entry should no longer
    # be considered fresh.
    assert cache.get(
        "city:paris"
    ) is None


def test_weather_cache_expires_after_ttl(monkeypatch):
    cache, clock = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "city:paris",
        b"payload",
    )

    clock.advance(61)

    assert cache.get(
        "city:paris"
    ) is None


def test_expired_entry_is_removed(monkeypatch):
    cache, clock = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "city:paris",
        b"payload",
    )

    assert "city:paris" in cache.entries

    clock.advance(61)

    assert cache.get(
        "city:paris"
    ) is None

    assert "city:paris" not in cache.entries


def test_weather_cache_evicts_oldest_when_over_capacity(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        ttl=3600,
        max_entries=2,
    )

    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.put("c", b"3")

    assert cache.get("a") is None
    assert cache.get("b") == b"2"
    assert cache.get("c") == b"3"


def test_weather_cache_get_refreshes_recency(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        ttl=3600,
        max_entries=2,
    )

    cache.put("a", b"1")
    cache.put("b", b"2")

    # Touch "a", making "b" the least-recently-used entry.
    assert cache.get("a") == b"1"

    cache.put("c", b"3")

    assert cache.get("a") == b"1"
    assert cache.get("b") is None
    assert cache.get("c") == b"3"


def test_weather_cache_update_refreshes_recency(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        ttl=3600,
        max_entries=2,
    )

    cache.put("a", b"old")
    cache.put("b", b"2")

    cache.put("a", b"new")

    cache.put("c", b"3")

    assert cache.get("a") == b"new"
    assert cache.get("b") is None
    assert cache.get("c") == b"3"


def test_weather_cache_accepts_empty_payload(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "empty",
        b"",
    )

    assert cache.get(
        "empty"
    ) == b""


def test_weather_cache_respects_maximum_entry_count(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        ttl=3600,
        max_entries=3,
    )

    for index in range(20):
        cache.put(
            f"key-{index}",
            str(index).encode(),
        )

    assert len(cache.entries) == 3


# ============================================================================
# Cache stampede protection
# ============================================================================

def test_weather_cache_begin_load_allows_first_loader(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    assert "city:paris" in cache.loading


def test_weather_cache_begin_load_blocks_duplicate_loader(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    # A second concurrent request should not start another upstream request.
    assert cache.begin_load(
        "city:paris"
    ) is False


def test_weather_cache_finish_load_releases_key(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    cache.finish_load(
        "city:paris"
    )

    assert "city:paris" not in cache.loading

    # A subsequent request can now become the loader.
    assert cache.begin_load(
        "city:paris"
    ) is True


def test_weather_cache_put_releases_loading_state(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    cache.put(
        "city:paris",
        b"weather",
    )

    assert "city:paris" not in cache.loading

    assert cache.get(
        "city:paris"
    ) == b"weather"


def test_weather_cache_wait_returns_loaded_value(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    # Simulate another request completing the upstream call before
    # the waiting request checks the cache.
    cache.put(
        "city:paris",
        b"weather",
    )

    result = cache.wait_for_load(
        "city:paris",
        timeout=0.01,
    )

    assert result == b"weather"


def test_weather_cache_wait_returns_none_after_failed_load(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    # Simulate an upstream failure.
    cache.finish_load(
        "city:paris"
    )

    result = cache.wait_for_load(
        "city:paris",
        timeout=0.01,
    )

    assert result is None


def test_weather_cache_loading_state_is_not_evicted(
    monkeypatch,
):
    cache, _ = make_weather_cache(
        monkeypatch,
        max_entries=1,
    )

    assert cache.begin_load(
        "city:paris"
    ) is True

    cache.put(
        "city:london",
        b"london",
    )

    # Loading state is independent of the LRU response cache.
    assert "city:paris" in cache.loading


# ============================================================================
# Rate limiter
# ============================================================================

def make_rate_limiter(
    monkeypatch,
    *,
    start: float = 1_000_000.0,
    max_requests: int = 3,
    window: float = 60.0,
    max_clients: int = 100,
    sweep_interval: float = 300.0,
):
    """
    Create a rate limiter with deterministic monotonic time.
    """

    clock = _FakeMonotonicClock(
        start=start
    )

    monkeypatch.setattr(
        skycast_server.time,
        "monotonic",
        clock,
    )

    limiter = skycast_server.RateLimiter(
        maximum=max_requests,
        window=window,
        max_clients=max_clients,
        sweep_interval=sweep_interval,
    )

    return limiter, clock


def test_rate_limiter_allows_up_to_max(
    monkeypatch,
):
    limiter, _ = make_rate_limiter(
        monkeypatch,
        max_requests=3,
        window=60,
    )

    results = [
        limiter.allow("1.2.3.4")[0]
        for _ in range(3)
    ]

    assert results == [
        True,
        True,
        True,
    ]

    allowed, retry_after = limiter.allow(
        "1.2.3.4"
    )

    assert allowed is False
    assert retry_after > 0


def test_rate_limiter_retry_after_is_reasonable(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=1,
        window=60,
    )

    assert limiter.allow(
        "client"
    )[0] is True

    allowed, retry_after = limiter.allow(
        "client"
    )

    assert allowed is False
    assert 0 < retry_after <= 60

    clock.advance(30)

    allowed, retry_after = limiter.allow(
        "client"
    )

    assert allowed is False
    assert 0 < retry_after <= 30


def test_rate_limiter_tracks_clients_independently(
    monkeypatch,
):
    limiter, _ = make_rate_limiter(
        monkeypatch,
        max_requests=1,
        window=60,
    )

    assert limiter.allow(
        "client-a"
    )[0] is True

    assert limiter.allow(
        "client-a"
    )[0] is False

    assert limiter.allow(
        "client-b"
    )[0] is True


def test_rate_limiter_recovers_after_window_elapses(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=1,
        window=60,
    )

    assert limiter.allow(
        "client-a"
    )[0] is True

    assert limiter.allow(
        "client-a"
    )[0] is False

    clock.advance(60)

    assert limiter.allow(
        "client-a"
    )[0] is True


def test_rate_limiter_old_hits_are_removed(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=2,
        window=60,
    )

    assert limiter.allow(
        "client-a"
    )[0] is True

    clock.advance(61)

    # The old hit should no longer count.
    assert limiter.allow(
        "client-a"
    )[0] is True


def test_rate_limiter_sliding_window_preserves_recent_hits(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=2,
        window=60,
    )

    assert limiter.allow(
        "client"
    )[0] is True

    clock.advance(30)

    assert limiter.allow(
        "client"
    )[0] is True

    # The first hit is still inside the 60-second window.
    allowed, _ = limiter.allow(
        "client"
    )

    assert allowed is False

    # Move beyond the first hit but not the second.
    clock.advance(31)

    assert limiter.allow(
        "client"
    )[0] is True


def test_rate_limiter_sweep_drops_stale_buckets(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=5,
        window=10,
        sweep_interval=30,
    )

    assert limiter.allow(
        "stale-client"
    )[0] is True

    assert "stale-client" in limiter.hits

    clock.advance(31)

    # Any subsequent call can trigger the sweep.
    assert limiter.allow(
        "other-client"
    )[0] is True

    assert "stale-client" not in limiter.hits


def test_rate_limiter_does_not_sweep_active_client(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=5,
        window=60,
        sweep_interval=30,
    )

    assert limiter.allow(
        "active-client"
    )[0] is True

    clock.advance(31)

    # Refresh the active bucket before the sweep.
    assert limiter.allow(
        "active-client"
    )[0] is True

    clock.advance(1)

    assert limiter.allow(
        "other-client"
    )[0] is True

    assert "active-client" in limiter.hits


def test_rate_limiter_enforces_maximum_client_buckets(
    monkeypatch,
):
    limiter, _ = make_rate_limiter(
        monkeypatch,
        max_requests=5,
        window=60,
        max_clients=3,
        sweep_interval=300,
    )

    assert limiter.allow("client-1")[0] is True
    assert limiter.allow("client-2")[0] is True
    assert limiter.allow("client-3")[0] is True

    # The implementation should not allow unlimited growth of the
    # client-tracking dictionary.
    allowed, retry_after = limiter.allow(
        "client-4"
    )

    assert allowed is False
    assert retry_after > 0

    assert len(limiter.hits) <= 3


def test_rate_limiter_empty_buckets_are_removed(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=5,
        window=10,
        sweep_interval=20,
    )

    assert limiter.allow(
        "client"
    )[0] is True

    clock.advance(21)

    # Trigger cleanup.
    limiter.allow(
        "other"
    )

    assert "client" not in limiter.hits


# ============================================================================
# Monotonic-clock regression tests
# ============================================================================

def test_cache_uses_monotonic_clock(
    monkeypatch,
):
    """
    Regression test ensuring cache expiration does not depend on time.time().
    """

    cache, clock = make_weather_cache(
        monkeypatch,
        ttl=60,
    )

    cache.put(
        "city:test",
        b"payload",
    )

    # Change wall-clock time drastically.
    wall_clock = skycast_server.time.time

    monkeypatch.setattr(
        skycast_server.time,
        "time",
        lambda: 999_999_999_999.0,
    )

    # TTL behavior must remain controlled by monotonic time.
    assert cache.get(
        "city:test"
    ) == b"payload"

    clock.advance(61)

    assert cache.get(
        "city:test"
    ) is None

    # Keep the local reference alive to make the intent explicit.
    assert callable(wall_clock)


def test_rate_limiter_uses_monotonic_clock(
    monkeypatch,
):
    limiter, clock = make_rate_limiter(
        monkeypatch,
        max_requests=1,
        window=60,
    )

    assert limiter.allow(
        "client"
    )[0] is True

    monkeypatch.setattr(
        skycast_server.time,
        "time",
        lambda: 999_999_999_999.0,
    )

    assert limiter.allow(
        "client"
    )[0] is False

    clock.advance(61)

    assert limiter.allow(
        "client"
    )[0] is True


# ============================================================================
# Sanity checks for standard-library dependencies
# ============================================================================

def test_monotonic_clock_is_available():
    assert callable(time.monotonic)
```
