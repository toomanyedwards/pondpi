import time

import pytest

from pondpi.sensors import discover_sensor_types
from pondpi.sensors.a02yyuw_sensor import create
from pondpi.sensors.base import Sensor


def test_discover_sensor_types_finds_all_built_ins():
    assert discover_sensor_types() == {"a02yyuw": create}


class _StubSensor(Sensor):
    """Minimal concrete Sensor -- no polling loop of its own (the
    base class has no notion of one), so tests drive
    is_healthy()/last_reading_monotonic() directly with no thread to
    race at all."""

    def __init__(self, stale_threshold_s=None):
        if stale_threshold_s is not None:
            self.STALE_READING_THRESHOLD_S = stale_threshold_s
        super().__init__()

    def read(self):
        return {}

    def reset_hardware(self):
        pass


def test_is_healthy_true_before_first_reading():
    sensor = _StubSensor()
    assert sensor.last_reading_monotonic() is None
    assert sensor.is_healthy() is True


def test_is_healthy_true_when_reading_recent():
    sensor = _StubSensor(stale_threshold_s=10)
    with sensor._lock:
        sensor._last_reading_monotonic = time.monotonic()

    assert sensor.is_healthy() is True


def test_is_healthy_false_when_reading_older_than_its_own_threshold():
    sensor = _StubSensor(stale_threshold_s=0.05)
    with sensor._lock:
        sensor._last_reading_monotonic = time.monotonic() - 1

    assert sensor.is_healthy() is False


def test_stale_reading_threshold_s_defaults_to_three_seconds():
    assert Sensor.STALE_READING_THRESHOLD_S == 3.0


def test_last_reset_at_is_none_before_any_reset():
    sensor = _StubSensor()
    assert sensor.last_reset_at() is None


def test_last_reset_at_updates_after_reset():
    sensor = _StubSensor()
    sensor.reset()
    assert sensor.last_reset_at() is not None


def test_last_reading_is_none_before_any_reading():
    sensor = _StubSensor()
    assert sensor.last_reading("raw") is None


def test_last_reading_returns_the_most_recently_recorded_value_for_its_key():
    sensor = _StubSensor()
    sensor._record_reading({"raw": 101})

    result = sensor.last_reading("raw")

    assert result["value"] == 101
    assert "at" in result


def test_last_reading_is_independent_per_key():
    sensor = _StubSensor()
    sensor._record_reading({"raw": 101})

    assert sensor.last_reading("processed") is None


def test_record_reading_updates_health_tracking_too():
    sensor = _StubSensor()
    assert sensor.last_reading_monotonic() is None

    sensor._record_reading({"raw": 101})

    assert sensor.last_reading_monotonic() is not None


def test_reset_clears_last_reading_cache():
    sensor = _StubSensor()
    sensor._record_reading({"raw": 101})

    sensor.reset()

    assert sensor.last_reading("raw") is None


def _write_fake_flat_package(tmp_path, package_name, module_filename, module_source):
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / f"{module_filename}.py").write_text(module_source)


def test_discover_sensor_types_raises_when_module_has_no_sensor_class(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_flat_package(tmp_path, "fakesensors_none", "empty_sensor", "x = 1\n")

    import fakesensors_none

    with pytest.raises(ValueError, match="found 0"):
        discover_sensor_types(fakesensors_none)


def test_discover_sensor_types_raises_when_module_has_multiple_sensor_classes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = (
        "from pondpi.sensors.base import Sensor\n\n"
        "class A(Sensor):\n    def read(self):\n        return {}\n\n"
        "class B(Sensor):\n    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return A()\n"
    )
    _write_fake_flat_package(tmp_path, "fakesensors_multi", "broken_sensor", source)

    import fakesensors_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_sensor_types(fakesensors_multi)


def test_discover_sensor_types_strips_the_sensor_suffix_for_the_type_name(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = (
        "from pondpi.sensors.base import Sensor\n\n"
        "class Thing(Sensor):\n    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return Thing()\n"
    )
    _write_fake_flat_package(tmp_path, "fakesensors_suffix", "widget_sensor", source)

    import fakesensors_suffix

    assert set(discover_sensor_types(fakesensors_suffix)) == {"widget"}


def test_discover_sensor_types_ignores_modules_not_ending_in_sensor(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    package_dir = tmp_path / "fakesensors_ignored"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "base.py").write_text("x = 1\n")
    (package_dir / "helpers.py").write_text("x = 1\n")

    import fakesensors_ignored

    # Neither base.py nor an arbitrary non-"_sensor" helper module is
    # scanned -- no hardcoded skip-list needed, just the naming convention.
    assert discover_sensor_types(fakesensors_ignored) == {}


def test_discover_sensor_types_finds_a_directory_package_with_class_defined_in_init(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    package_dir = tmp_path / "fakesensors_dirinit"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    driver_dir = package_dir / "widget_sensor"
    driver_dir.mkdir()
    (driver_dir / "__init__.py").write_text(
        "from pondpi.sensors.base import Sensor\n\n"
        "class Widget(Sensor):\n"
        "    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return Widget()\n"
    )

    import fakesensors_dirinit

    registry = discover_sensor_types(fakesensors_dirinit)
    assert set(registry) == {"widget"}
    assert registry["widget"]({}, simulate=True).read() == {}


def test_discover_sensor_types_finds_a_directory_package_re_exporting_from_a_submodule(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    package_dir = tmp_path / "fakesensors_dirsub"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    driver_dir = package_dir / "widget_sensor"
    driver_dir.mkdir()
    (driver_dir / "driver.py").write_text(
        "from pondpi.sensors.base import Sensor\n\n"
        "class Widget(Sensor):\n"
        "    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return Widget()\n"
    )
    (driver_dir / "__init__.py").write_text("from .driver import Widget, create\n")

    import fakesensors_dirsub

    registry = discover_sensor_types(fakesensors_dirsub)
    assert set(registry) == {"widget"}
    assert registry["widget"]({}, simulate=True).read() == {}
