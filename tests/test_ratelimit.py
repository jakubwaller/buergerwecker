"""IPRateLimiter must not grow by one entry per client address forever."""
from app import ratelimit as rl


def test_stale_keys_are_swept(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(rl.time, "time", lambda: clock[0])
    lim = rl.IPRateLimiter()
    lim.sweep_every = 50
    for i in range(200):
        assert lim.hit(f"ip:203.0.113.{i}", 5, 3600)
    assert lim.tracked_keys() == 200
    # Everything above is older than the window now; the next sweep drops it.
    clock[0] += 3601
    for i in range(50):
        lim.hit(f"ip:198.51.100.{i}", 5, 3600)
    assert lim.tracked_keys() <= 50


def test_sweep_keeps_keys_that_still_count(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(rl.time, "time", lambda: clock[0])
    lim = rl.IPRateLimiter()
    lim.sweep_every = 10
    assert lim.hit("ip:a", 1, 3600)
    clock[0] += 1800
    for _ in range(20):
        lim.hit("ip:b", 100, 3600)
    # "a" is inside its window and still over its limit: not swept, still blocked.
    assert not lim.hit("ip:a", 1, 3600)
