import app.health as health


def test_uptime_is_zero_before_mark_started(monkeypatch):
    monkeypatch.setattr(health, "_started_at", None)
    assert health.uptime_seconds() == 0.0


def test_uptime_increases_after_mark_started(monkeypatch):
    health.mark_started()
    first = health.uptime_seconds()
    second = health.uptime_seconds()
    assert second >= first >= 0.0


def test_last_search_sweep_at_is_none_before_any_sweep(monkeypatch):
    monkeypatch.setattr(health, "_last_search_sweep_at_iso", None)
    assert health.last_search_sweep_at() is None


def test_record_search_sweep_sets_an_iso_timestamp(monkeypatch):
    monkeypatch.setattr(health, "_last_search_sweep_at_iso", None)
    health.record_search_sweep()
    recorded = health.last_search_sweep_at()
    assert recorded is not None
    # Round-trips through datetime.fromisoformat, same as
    # app.api.monitoring.detailed_health does when checking overdue-ness.
    from datetime import datetime

    datetime.fromisoformat(recorded)
