import pytest

from pondpi.signals import discover_signal_types
from pondpi.signals.exponential_smoothing_signal import ExponentialSmoothingSignal
from pondpi.signals.rolling_average_signal import RollingAverageSignal
from pondpi.signals.rolling_median_signal import RollingMedianSignal
from pondpi.signals.sensor_signal import SensorSignal


def test_sensor_signal_passes_through_unchanged():
    signal = SensorSignal(sensor="pond_main")
    assert signal.add(101) == 101
    assert signal.add(999) == 999


def test_sensor_signal_extra_state_reports_its_sensor_and_mode():
    signal = SensorSignal(sensor="pond_main")
    assert signal.extra_state() == {"sensor": "pond_main", "mode": "raw"}


def test_sensor_signal_extra_state_reports_explicit_mode():
    signal = SensorSignal(sensor="pond_main", mode="processed")
    assert signal.extra_state() == {"sensor": "pond_main", "mode": "processed"}


def test_rolling_median_signal_delegates_to_rolling_median_filter():
    signal = RollingMedianSignal(window_size=3)
    signal.add(10)
    signal.add(30)
    assert signal.add(20) == 20  # median of 10, 30, 20


def test_rolling_median_signal_extra_state():
    signal = RollingMedianSignal(window_size=3)
    signal.add(10)
    assert signal.extra_state() == {"window_size": 3, "samples_in_window": 1}


def test_rolling_average_signal_delegates_to_rolling_average():
    signal = RollingAverageSignal(window_size=2)
    signal.add(10)
    assert signal.add(20) == 15


def test_rolling_average_signal_extra_state():
    signal = RollingAverageSignal(window_size=2)
    signal.add(10)
    assert signal.extra_state() == {"window_size": 2, "samples_in_window": 1}


def test_exponential_smoothing_signal_first_reading_passes_through():
    signal = ExponentialSmoothingSignal(alpha=0.5)
    assert signal.add(10) == 10


def test_exponential_smoothing_signal_weights_new_reading_by_alpha():
    signal = ExponentialSmoothingSignal(alpha=0.5)
    signal.add(10)
    assert signal.add(20) == 15  # 0.5 * 20 + 0.5 * 10
    assert signal.add(20) == 17.5  # 0.5 * 20 + 0.5 * 15


def test_exponential_smoothing_signal_extra_state():
    signal = ExponentialSmoothingSignal(alpha=0.3)
    signal.add(10)
    assert signal.extra_state() == {"alpha": 0.3}


def test_discover_signal_types_finds_all_built_ins():
    signal_types = discover_signal_types()

    assert signal_types == {
        "sensor": SensorSignal,
        "rolling_median": RollingMedianSignal,
        "rolling_average": RollingAverageSignal,
        "exponential_smoothing": ExponentialSmoothingSignal,
    }


def _write_fake_package(tmp_path, package_name, module_filename, module_source):
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / f"{module_filename}.py").write_text(module_source)


def test_discover_signal_types_raises_when_module_has_no_signal_class(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_none", "empty_signal", "x = 1\n")

    import fakepkg_none

    with pytest.raises(ValueError, match="found 0"):
        discover_signal_types(fakepkg_none)


def test_discover_signal_types_raises_when_module_has_multiple_signal_classes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = (
        "from pondpi.signals.base import LevelSignal\n\n"
        "class A(LevelSignal):\n    pass\n\n"
        "class B(LevelSignal):\n    pass\n"
    )
    _write_fake_package(tmp_path, "fakepkg_multi", "broken_signal", source)

    import fakepkg_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_signal_types(fakepkg_multi)


def test_discover_signal_types_strips_the_signal_suffix_for_the_type_name(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = "from pondpi.signals.base import LevelSignal\n\nclass Thing(LevelSignal):\n    pass\n"
    _write_fake_package(tmp_path, "fakepkg_suffix", "widget_signal", source)

    import fakepkg_suffix

    assert set(discover_signal_types(fakepkg_suffix)) == {"widget"}


def test_discover_signal_types_ignores_modules_not_ending_in_signal(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_ignored", "base", "x = 1\n")
    (tmp_path / "fakepkg_ignored" / "helpers.py").write_text("x = 1\n")

    import fakepkg_ignored

    # Neither base.py nor an arbitrary non-"_signal" helper module is
    # scanned -- no hardcoded skip-list needed, just the naming convention.
    assert discover_signal_types(fakepkg_ignored) == {}
