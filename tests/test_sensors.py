import pytest

from pondpi.sensors import discover_sensor_types
from pondpi.sensors.a02yyuw_sensor import create


def test_discover_sensor_types_finds_all_built_ins():
    assert discover_sensor_types() == {"a02yyuw": create}


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
        "from pondpi.sensors.base import LevelSensor\n\n"
        "class A(LevelSensor):\n    def read(self):\n        return {}\n\n"
        "class B(LevelSensor):\n    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return A()\n"
    )
    _write_fake_flat_package(tmp_path, "fakesensors_multi", "broken_sensor", source)

    import fakesensors_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_sensor_types(fakesensors_multi)


def test_discover_sensor_types_strips_the_sensor_suffix_for_the_type_name(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = (
        "from pondpi.sensors.base import LevelSensor\n\n"
        "class Thing(LevelSensor):\n    def read(self):\n        return {}\n\n"
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
        "from pondpi.sensors.base import LevelSensor\n\n"
        "class Widget(LevelSensor):\n"
        "    def __init__(self):\n"
        "        super().__init__(on_reading=lambda k, v: None, poll_interval_s=1000)\n"
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
        "from pondpi.sensors.base import LevelSensor\n\n"
        "class Widget(LevelSensor):\n"
        "    def __init__(self):\n"
        "        super().__init__(on_reading=lambda k, v: None, poll_interval_s=1000)\n"
        "    def read(self):\n        return {}\n\n"
        "def create(params, simulate):\n    return Widget()\n"
    )
    (driver_dir / "__init__.py").write_text("from .driver import Widget, create\n")

    import fakesensors_dirsub

    registry = discover_sensor_types(fakesensors_dirsub)
    assert set(registry) == {"widget"}
    assert registry["widget"]({}, simulate=True).read() == {}
