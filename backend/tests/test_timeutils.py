from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.timeutils import utcnow


def test_utcnow_returns_naive_datetime():
    assert utcnow().tzinfo is None


def test_utcnow_strips_tzinfo_without_changing_the_instant():
    fixed = datetime(2026, 7, 29, 12, 30, 0, tzinfo=UTC)
    with patch("app.timeutils.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed
        result = utcnow()
    mock_datetime.now.assert_called_once_with(UTC)
    assert result == datetime(2026, 7, 29, 12, 30, 0)
    assert result.tzinfo is None


def test_utcnow_is_close_to_real_time():
    delta = abs(utcnow() - datetime.now(UTC).replace(tzinfo=None))
    assert delta < timedelta(seconds=5)


def test_two_utcnow_calls_are_comparable_without_raising():
    # The bug this guards against: comparing a naive datetime against an
    # aware one raises TypeError. Both sides here must be naive.
    first = utcnow()
    second = utcnow()
    assert (second - first) >= timedelta(0)
