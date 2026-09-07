import time

import pytest

from pondpi.signals import discover_signal_types
from pondpi.signals.exponential_smoothing_signal import ExponentialSmoothingSignal
from pondpi.signals.rolling_average_signal import RollingAverageSignal
from pondpi.signals.rolling_median_signal import RollingMedianSignal
from pondpi.signals.sensor_signal import SensorSignal


class _FakeSensor:
    """Stand-in for a Sensor instance -- exposes just the pull surface
    (`read()`) a `reads_from_sensor` signal actually uses. `read_calls`
    records each `settings` a caller passed in, for tests that check
    what a Signal forwards."""

    def __init__(self, readings=None):
        self._readings = readings or {}
        self.read_calls = []

    def read(self, settings=None):
        self.read_calls.append(settings)
        mode = (settings or {}).get("mode", "raw")
        return self._readings.get(mode)


class _FakeSourceSignal:
    """Stand-in for a live source Signal -- `.set()` controls what the
    next `.read()` returns, mirroring how a real signal's cache changes
    over time."""

    def __init__(self, result=None):
        self._result = result

    def set(self, value, at):
        self._result = {"value": value, "at": at}

    def read(self):
        return self._result


def test_sensor_signal_converts_mm_into_its_declared_unit():
    signal = SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main", unit="cm")
    assert signal.add(101) == 10.1
    assert signal.add(999) == 99.9


def test_sensor_signal_rejects_unsupported_unit():
    with pytest.raises(ValueError, match="invalid 'unit' 'mm'"):
        SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main", unit="mm")


def test_sensor_signal_rejects_missing_unit():
    with pytest.raises(ValueError, match="missing required 'unit'"):
        SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main")


def test_sensor_signal_rejects_unknown_sensor():
    with pytest.raises(ValueError, match="invalid or missing 'source' 'rain_barrel'"):
        SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="rain_barrel", unit="cm")


def test_sensor_signal_rejects_unsupported_mode():
    with pytest.raises(ValueError, match="invalid 'mode' 'smoothed'"):
        SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main", unit="cm", mode="smoothed")


def test_sensor_signal_extra_state_reports_its_sensor_and_mode():
    signal = SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main", unit="cm")
    assert signal.extra_state() == {"sensor": "pond_main", "mode": "raw"}


def test_sensor_signal_extra_state_reports_explicit_mode():
    signal = SensorSignal(sensor_objects={"pond_main": _FakeSensor()}, sensor="pond_main", unit="cm", mode="processed")
    assert signal.extra_state() == {"sensor": "pond_main", "mode": "processed"}


def test_sensor_signal_read_pulls_its_own_mode_from_the_sensor():
    sensor = _FakeSensor({"raw": {"value": 101, "at": "t1"}, "processed": {"value": 202, "at": "t2"}})
    raw_signal = SensorSignal(sensor_objects={"pond_main": sensor}, sensor="pond_main", unit="cm", mode="raw")
    processed_signal = SensorSignal(
        sensor_objects={"pond_main": sensor}, sensor="pond_main", unit="cm", mode="processed"
    )

    assert raw_signal.read() == {"value": 10.1, "at": "t1", "sensor": "pond_main", "mode": "raw"}
    assert processed_signal.read() == {"value": 20.2, "at": "t2", "sensor": "pond_main", "mode": "processed"}


def test_sensor_signal_read_is_none_before_the_sensor_has_a_reading():
    sensor = _FakeSensor()
    signal = SensorSignal(sensor_objects={"pond_main": sensor}, sensor="pond_main", unit="cm")
    assert signal.read() is None


def test_sensor_signal_passes_its_own_mode_as_the_sensors_read_settings():
    # The sensor doesn't know or care about "mode" as a concept of its
    # own -- it's purely the caller's (this signal's) settings, passed
    # straight through to Sensor.read().
    sensor = _FakeSensor({"processed": {"value": 202, "at": "t2"}})
    signal = SensorSignal(sensor_objects={"pond_main": sensor}, sensor="pond_main", unit="cm", mode="processed")

    signal.read()

    assert sensor.read_calls == [{"mode": "processed"}]


def test_rolling_median_signal_delegates_to_rolling_median_filter():
    signal = RollingMedianSignal(window_size=3)
    signal.add(10)
    signal.add(30)
    assert signal.add(20) == 20  # median of 10, 30, 20


def test_rolling_median_signal_extra_state():
    signal = RollingMedianSignal(window_size=3)
    signal.add(10)
    assert signal.extra_state() == {"window_size": 3, "samples_in_window": 1}


def test_rolling_median_signal_reset_clears_accumulated_window():
    signal = RollingMedianSignal(window_size=3)
    signal.add(10)
    signal.add(30)

    signal.reset()

    assert signal.extra_state() == {"window_size": 3, "samples_in_window": 0}
    assert signal.add(20) == 20  # median of just [20] -- old readings gone


def test_signal_read_returns_none_before_source_has_any_value():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)
    assert signal.read() is None


def test_signal_read_pulls_and_computes_from_its_source():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)

    source.set(10, "t1")
    result = signal.read()

    assert result["value"] == 10
    assert result["samples_in_window"] == 1


def test_signal_read_propagates_the_sources_at_not_its_own_read_time():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)

    source.set(10, "2020-01-01T00:00:00+00:00")

    assert signal.read()["at"] == "2020-01-01T00:00:00+00:00"


def test_signal_read_does_not_double_add_when_source_is_unchanged():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)

    source.set(10, "t1")
    signal.read()
    signal.read()
    signal.read()

    # Still just one sample in the window -- three read()s with no new
    # upstream data must not add() three times.
    assert signal.read()["samples_in_window"] == 1


def test_signal_read_adds_again_once_source_advances():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)

    source.set(10, "t1")
    signal.read()
    source.set(20, "t2")
    signal.read()

    assert signal.read()["samples_in_window"] == 2


def test_signal_reset_clears_dirty_check_state():
    source = _FakeSourceSignal()
    signal = RollingMedianSignal(window_size=3, source_signal=source)

    source.set(10, "t1")
    signal.read()
    signal.reset()
    # Same source value/timestamp as before the reset -- without reset()
    # clearing its "already incorporated" bookkeeping, this would be
    # (wrongly) treated as already-seen and skipped.
    result = signal.read()

    assert result["samples_in_window"] == 1
    assert result["value"] == 10


def _wait_until(predicate, timeout_s=1):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)


def _fixed_source(value):
    return _FakeSourceSignal(None if value is None else {"value": value, "at": "at"})


def _queue_source(values):
    queue = list(values)

    class _Source:
        def read(self):
            value = queue.pop(0) if queue else None
            return None if value is None else {"value": value, "at": "at"}

    return _Source()


def test_rolling_average_signal_current_is_none_before_first_reading():
    signal = RollingAverageSignal(window_size=5, poll_interval_ms=10, source_signal=_fixed_source(None))
    assert signal.read() is None


def test_rolling_average_signal_polls_its_source_and_populates_current():
    # Its background thread starts the moment it's constructed -- no
    # separate start() call needed.
    signal = RollingAverageSignal(window_size=5, poll_interval_ms=10, source_signal=_fixed_source(100))
    _wait_until(lambda: signal.read() is not None)

    result = signal.read()
    assert result["value"] == 100
    assert result["window_size"] == 5
    assert result["poll_interval_ms"] == 10
    assert "at" in result


def test_rolling_average_signal_averages_over_its_window():
    signal = RollingAverageSignal(
        window_size=3, poll_interval_ms=10, source_signal=_queue_source([10, 20, 30] + [30] * 100)
    )
    _wait_until(lambda: signal.read() is not None and signal.read()["samples_in_window"] == 3)

    result = signal.read()
    assert result["value"] == 20  # (10 + 20 + 30) / 3
    assert result["samples_in_window"] == 3


def test_rolling_average_signal_ignores_a_none_reading():
    signal = RollingAverageSignal(
        window_size=5, poll_interval_ms=10, source_signal=_queue_source([None, None, 100] + [100] * 100)
    )
    _wait_until(lambda: signal.read() is not None)

    assert signal.read()["value"] == 100


def test_rolling_average_signal_reset_clears_accumulated_window():
    signal = RollingAverageSignal(window_size=5, poll_interval_ms=15, source_signal=_fixed_source(10))
    _wait_until(lambda: signal.read() is not None and signal.read()["samples_in_window"] >= 3)

    signal.reset()

    # Polling resumes on its own (no external re-trigger needed) with a
    # genuinely fresh window -- samples_in_window starts back at 1, not
    # continuing to grow from before reset().
    _wait_until(lambda: signal.read() is not None)
    assert signal.read()["samples_in_window"] == 1


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


def test_exponential_smoothing_signal_reset_clears_accumulated_average():
    signal = ExponentialSmoothingSignal(alpha=0.5)
    signal.add(10)
    signal.add(20)

    signal.reset()

    assert signal.add(50) == 50  # first reading after reset passes through unchanged


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
        "from pondpi.signals.base import Signal\n\n"
        "class A(Signal):\n    pass\n\n"
        "class B(Signal):\n    pass\n"
    )
    _write_fake_package(tmp_path, "fakepkg_multi", "broken_signal", source)

    import fakepkg_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_signal_types(fakepkg_multi)


def test_discover_signal_types_strips_the_signal_suffix_for_the_type_name(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = "from pondpi.signals.base import Signal\n\nclass Thing(Signal):\n    pass\n"
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
