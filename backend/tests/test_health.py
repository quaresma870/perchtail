import app.health as health


def test_uptime_is_zero_before_mark_started(monkeypatch):
    monkeypatch.setattr(health, "_started_at", None)
    assert health.uptime_seconds() == 0.0


def test_uptime_increases_after_mark_started(monkeypatch):
    health.mark_started()
    first = health.uptime_seconds()
    second = health.uptime_seconds()
    assert second >= first >= 0.0

