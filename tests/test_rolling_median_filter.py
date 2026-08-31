from pondpi.signal_processors.utils.rolling_median_filter import RollingMedianFilter


def test_median_is_none_before_any_reading():
    mf = RollingMedianFilter(window_size=5)
    assert mf.median is None
    assert mf.count == 0


def test_median_of_single_value():
    mf = RollingMedianFilter(window_size=5)
    assert mf.add(100) == 100


def test_median_of_odd_number_of_values():
    mf = RollingMedianFilter(window_size=5)
    mf.add(10)
    mf.add(30)
    assert mf.add(20) == 20


def test_median_of_even_number_of_values_averages_middle_two():
    mf = RollingMedianFilter(window_size=5)
    mf.add(10)
    mf.add(30)
    mf.add(20)
    assert mf.add(40) == 25  # sorted: 10, 20, 30, 40 -> (20 + 30) / 2


def test_median_ignores_input_order():
    mf = RollingMedianFilter(window_size=5)
    mf.add(30)
    mf.add(10)
    assert mf.add(20) == 20


def test_window_evicts_oldest_reading():
    mf = RollingMedianFilter(window_size=3)
    mf.add(10)
    mf.add(20)
    mf.add(30)
    assert mf.add(1000) == 30  # 10 evicted, sorted: 20, 30, 1000 -> 30


def test_count_caps_at_window_size():
    mf = RollingMedianFilter(window_size=2)
    mf.add(1)
    mf.add(2)
    mf.add(3)
    assert mf.count == 2


def test_default_window_size_is_five():
    mf = RollingMedianFilter()
    assert mf.window_size == 5
