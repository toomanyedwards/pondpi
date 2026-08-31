from level_strategies import (
    STRATEGY_TYPES,
    MedianStrategy,
    MedianThenRollingAverageStrategy,
    RawStrategy,
    RollingAverageStrategy,
)


def test_raw_strategy_passes_through_unchanged():
    strategy = RawStrategy()
    assert strategy.add(101) == 101
    assert strategy.add(999) == 999
    assert strategy.extra_state() == {}


def test_median_strategy_delegates_to_median_filter():
    strategy = MedianStrategy(window_size=3)
    strategy.add(10)
    strategy.add(30)
    assert strategy.add(20) == 20  # median of 10, 30, 20


def test_median_strategy_extra_state():
    strategy = MedianStrategy(window_size=3)
    strategy.add(10)
    assert strategy.extra_state() == {"window_size": 3, "samples_in_window": 1}


def test_rolling_average_strategy_delegates_to_rolling_average():
    strategy = RollingAverageStrategy(window_size=2)
    strategy.add(10)
    assert strategy.add(20) == 15


def test_rolling_average_strategy_extra_state():
    strategy = RollingAverageStrategy(window_size=2)
    strategy.add(10)
    assert strategy.extra_state() == {"window_size": 2, "samples_in_window": 1}


def test_median_then_rolling_average_strategy_chains_both_filters():
    strategy = MedianThenRollingAverageStrategy(median_window_size=3, rolling_window_size=2)

    strategy.add(10)  # median([10]) = 10 -> rolling([10]) = 10
    strategy.add(30)  # median([10, 30]) = 20 -> rolling([10, 20]) = 15

    # median([10, 30, 20]) = 20 -> rolling([20, 20]) = 20
    assert strategy.add(20) == 20

    # median([30, 20, 40]) = 30 -> rolling([20, 30]) = 25
    assert strategy.add(40) == 25


def test_median_then_rolling_average_strategy_extra_state():
    strategy = MedianThenRollingAverageStrategy(median_window_size=3, rolling_window_size=2)
    strategy.add(10)
    assert strategy.extra_state() == {
        "median_window_size": 3,
        "rolling_window_size": 2,
        "samples_in_rolling_window": 1,
    }


def test_strategy_types_registry_covers_all_built_in_strategies():
    assert set(STRATEGY_TYPES) == {"raw", "median", "rolling_average", "median_then_rolling_average"}
