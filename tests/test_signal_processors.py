import pytest

from pondpi.signal_processors import discover_signal_processor_types
from pondpi.signal_processors.median import MedianSignalProcessor
from pondpi.signal_processors.median_then_rolling_average import (
    MedianThenRollingAverageSignalProcessor,
)
from pondpi.signal_processors.raw import RawSignalProcessor
from pondpi.signal_processors.rolling_average import RollingAverageSignalProcessor


def test_raw_processor_passes_through_unchanged():
    processor = RawSignalProcessor()
    assert processor.add(101) == 101
    assert processor.add(999) == 999
    assert processor.extra_state() == {}


def test_median_processor_delegates_to_median_filter():
    processor = MedianSignalProcessor(window_size=3)
    processor.add(10)
    processor.add(30)
    assert processor.add(20) == 20  # median of 10, 30, 20


def test_median_processor_extra_state():
    processor = MedianSignalProcessor(window_size=3)
    processor.add(10)
    assert processor.extra_state() == {"window_size": 3, "samples_in_window": 1}


def test_rolling_average_processor_delegates_to_rolling_average():
    processor = RollingAverageSignalProcessor(window_size=2)
    processor.add(10)
    assert processor.add(20) == 15


def test_rolling_average_processor_extra_state():
    processor = RollingAverageSignalProcessor(window_size=2)
    processor.add(10)
    assert processor.extra_state() == {"window_size": 2, "samples_in_window": 1}


def test_median_then_rolling_average_processor_chains_both_filters():
    processor = MedianThenRollingAverageSignalProcessor(median_window_size=3, rolling_window_size=2)

    processor.add(10)  # median([10]) = 10 -> rolling([10]) = 10
    processor.add(30)  # median([10, 30]) = 20 -> rolling([10, 20]) = 15

    # median([10, 30, 20]) = 20 -> rolling([20, 20]) = 20
    assert processor.add(20) == 20

    # median([30, 20, 40]) = 30 -> rolling([20, 30]) = 25
    assert processor.add(40) == 25


def test_median_then_rolling_average_processor_extra_state():
    processor = MedianThenRollingAverageSignalProcessor(median_window_size=3, rolling_window_size=2)
    processor.add(10)
    assert processor.extra_state() == {
        "median_window_size": 3,
        "rolling_window_size": 2,
        "samples_in_rolling_window": 1,
    }


def test_discover_signal_processor_types_finds_all_built_ins():
    processor_types = discover_signal_processor_types()

    assert processor_types == {
        "raw": RawSignalProcessor,
        "median": MedianSignalProcessor,
        "rolling_average": RollingAverageSignalProcessor,
        "median_then_rolling_average": MedianThenRollingAverageSignalProcessor,
    }


def _write_fake_package(tmp_path, package_name, module_filename, module_source):
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / f"{module_filename}.py").write_text(module_source)


def test_discover_signal_processor_types_raises_when_module_has_no_processor_class(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_none", "empty", "x = 1\n")

    import fakepkg_none

    with pytest.raises(ValueError, match="found 0"):
        discover_signal_processor_types(fakepkg_none)


def test_discover_signal_processor_types_raises_when_module_has_multiple_processor_classes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = (
        "from pondpi.signal_processors.base import LevelSignalProcessor\n\n"
        "class A(LevelSignalProcessor):\n    pass\n\n"
        "class B(LevelSignalProcessor):\n    pass\n"
    )
    _write_fake_package(tmp_path, "fakepkg_multi", "broken", source)

    import fakepkg_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_signal_processor_types(fakepkg_multi)


def test_discover_signal_processor_types_skips_base_module(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_base_only", "base", "x = 1\n")

    import fakepkg_base_only

    assert discover_signal_processor_types(fakepkg_base_only) == {}
