from pondpi.signal_processors.utils.rolling_average import RollingAverage


def test_average_is_none_before_any_reading():
    avg = RollingAverage(window_size=5)
    assert avg.average is None
    assert avg.count == 0


def test_average_of_single_value():
    avg = RollingAverage(window_size=5)
    assert avg.add(100) == 100


def test_average_over_partial_window():
    avg = RollingAverage(window_size=3)
    avg.add(10)
    avg.add(20)
    assert avg.add(30) == 20


def test_window_evicts_oldest_reading():
    avg = RollingAverage(window_size=2)
    avg.add(10)
    avg.add(20)
    assert avg.add(30) == 25  # (20 + 30) / 2


def test_count_caps_at_window_size():
    avg = RollingAverage(window_size=2)
    avg.add(1)
    avg.add(2)
    avg.add(3)
    assert avg.count == 2
