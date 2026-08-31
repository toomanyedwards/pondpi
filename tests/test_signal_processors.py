import pytest

from pondpi.signal_processors import discover_signal_processor_types
from pondpi.signal_processors.chain_processor import ChainSignalProcessor
from pondpi.signal_processors.exponential_smoothing_processor import (
    ExponentialSmoothingSignalProcessor,
)
from pondpi.signal_processors.raw_processor import RawSignalProcessor
from pondpi.signal_processors.rolling_average_processor import (
    RollingAverageSignalProcessor,
)
from pondpi.signal_processors.rolling_median_processor import (
    RollingMedianSignalProcessor,
)


def test_raw_processor_passes_through_unchanged():
    processor = RawSignalProcessor()
    assert processor.add(101) == 101
    assert processor.add(999) == 999
    assert processor.extra_state() == {}


def test_rolling_median_processor_delegates_to_rolling_median_filter():
    processor = RollingMedianSignalProcessor(window_size=3)
    processor.add(10)
    processor.add(30)
    assert processor.add(20) == 20  # median of 10, 30, 20


def test_rolling_median_processor_extra_state():
    processor = RollingMedianSignalProcessor(window_size=3)
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


def test_exponential_smoothing_processor_first_reading_passes_through():
    processor = ExponentialSmoothingSignalProcessor(alpha=0.5)
    assert processor.add(10) == 10


def test_exponential_smoothing_processor_weights_new_reading_by_alpha():
    processor = ExponentialSmoothingSignalProcessor(alpha=0.5)
    processor.add(10)
    assert processor.add(20) == 15  # 0.5 * 20 + 0.5 * 10
    assert processor.add(20) == 17.5  # 0.5 * 20 + 0.5 * 15


def test_exponential_smoothing_processor_extra_state():
    processor = ExponentialSmoothingSignalProcessor(alpha=0.3)
    processor.add(10)
    assert processor.extra_state() == {"alpha": 0.3}


def test_chain_processor_feeds_each_step_output_into_the_next():
    chain = ChainSignalProcessor(
        steps=[
            ("rolling_median", RollingMedianSignalProcessor(window_size=3)),
            ("rolling_average", RollingAverageSignalProcessor(window_size=2)),
        ]
    )

    chain.add(10)  # median([10]) = 10 -> rolling([10]) = 10
    chain.add(30)  # median([10, 30]) = 20 -> rolling([10, 20]) = 15

    # median([10, 30, 20]) = 20 -> rolling([20, 20]) = 20
    assert chain.add(20) == 20

    # median([30, 20, 40]) = 30 -> rolling([20, 30]) = 25
    assert chain.add(40) == 25


def test_chain_processor_extra_state():
    chain = ChainSignalProcessor(
        steps=[
            ("rolling_median5", RollingMedianSignalProcessor(window_size=3)),
            ("rolling_average", RollingAverageSignalProcessor(window_size=2)),
        ]
    )
    chain.add(10)

    assert chain.extra_state() == {
        "steps": [
            {"processor": "rolling_median5", "window_size": 3, "samples_in_window": 1},
            {"processor": "rolling_average", "window_size": 2, "samples_in_window": 1},
        ]
    }


def test_discover_signal_processor_types_finds_all_built_ins():
    processor_types = discover_signal_processor_types()

    assert processor_types == {
        "raw": RawSignalProcessor,
        "rolling_median": RollingMedianSignalProcessor,
        "rolling_average": RollingAverageSignalProcessor,
        "exponential_smoothing": ExponentialSmoothingSignalProcessor,
        "chain": ChainSignalProcessor,
    }


def _write_fake_package(tmp_path, package_name, module_filename, module_source):
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / f"{module_filename}.py").write_text(module_source)


def test_discover_signal_processor_types_raises_when_module_has_no_processor_class(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_none", "empty_processor", "x = 1\n")

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
    _write_fake_package(tmp_path, "fakepkg_multi", "broken_processor", source)

    import fakepkg_multi

    with pytest.raises(ValueError, match="found 2"):
        discover_signal_processor_types(fakepkg_multi)


def test_discover_signal_processor_types_strips_the_processor_suffix_for_the_type_name(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    source = "from pondpi.signal_processors.base import LevelSignalProcessor\n\nclass Thing(LevelSignalProcessor):\n    pass\n"
    _write_fake_package(tmp_path, "fakepkg_suffix", "widget_processor", source)

    import fakepkg_suffix

    assert set(discover_signal_processor_types(fakepkg_suffix)) == {"widget"}


def test_discover_signal_processor_types_ignores_modules_not_ending_in_processor(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_fake_package(tmp_path, "fakepkg_ignored", "base", "x = 1\n")
    (tmp_path / "fakepkg_ignored" / "helpers.py").write_text("x = 1\n")

    import fakepkg_ignored

    # Neither base.py nor an arbitrary non-"_processor" helper module is
    # scanned -- no hardcoded skip-list needed, just the naming convention.
    assert discover_signal_processor_types(fakepkg_ignored) == {}
