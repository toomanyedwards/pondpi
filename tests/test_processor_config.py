import pytest

from pondpi.processor_config import load_processors
from pondpi.processors.median_then_rolling_average import (
    MedianThenRollingAverageProcessor,
)
from pondpi.processors.raw import RawProcessor


def write_yaml(tmp_path, content):
    path = tmp_path / "processors.yaml"
    path.write_text(content)
    return path


def test_loads_valid_config(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_avg
            type: median_then_rolling_average
            primary: true
            params:
              median_window_size: 5
              rolling_window_size: 40
          - name: instantaneous_raw
            type: raw
        """,
    )

    processors, primary_name = load_processors(path)

    assert primary_name == "rolling_avg"
    assert set(processors) == {"rolling_avg", "instantaneous_raw"}
    assert isinstance(processors["rolling_avg"], MedianThenRollingAverageProcessor)
    assert isinstance(processors["instantaneous_raw"], RawProcessor)


def test_missing_primary_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: raw
            type: raw
        """,
    )

    with pytest.raises(ValueError, match="primary"):
        load_processors(path)


def test_multiple_primaries_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: a
            type: raw
            primary: true
          - name: b
            type: raw
            primary: true
        """,
    )

    with pytest.raises(ValueError, match="multiple processors marked primary"):
        load_processors(path)


def test_unknown_type_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: a
            type: exponential_moving_average
            primary: true
        """,
    )

    with pytest.raises(ValueError, match="unknown type"):
        load_processors(path)


def test_duplicate_name_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: a
            type: raw
            primary: true
          - name: a
            type: raw
        """,
    )

    with pytest.raises(ValueError, match="duplicate processor name"):
        load_processors(path)


def test_invalid_params_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: a
            type: median
            primary: true
            params:
              not_a_real_param: 5
        """,
    )

    with pytest.raises(ValueError, match="invalid params"):
        load_processors(path)


def test_empty_processors_list_raises(tmp_path):
    path = write_yaml(tmp_path, "processors: []\n")

    with pytest.raises(ValueError, match="non-empty list"):
        load_processors(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_processors(tmp_path / "does_not_exist.yaml")
