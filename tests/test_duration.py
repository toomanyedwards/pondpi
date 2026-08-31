from duration import format_duration


def test_zero_seconds():
    assert format_duration(0) == "0s"


def test_seconds_only():
    assert format_duration(45) == "45s"


def test_minutes_and_seconds():
    assert format_duration(154) == "2m 34s"  # 2*60 + 34


def test_hours_minutes_and_seconds():
    assert format_duration(3661) == "1h 1m 1s"  # 1*3600 + 1*60 + 1


def test_days_hours_minutes_and_seconds():
    assert format_duration(90061) == "1d 1h 1m 1s"  # 86400 + 3600 + 60 + 1


def test_exact_day_includes_zero_units():
    assert format_duration(86400) == "1d 0h 0m 0s"


def test_truncates_fractional_seconds():
    assert format_duration(45.9) == "45s"
