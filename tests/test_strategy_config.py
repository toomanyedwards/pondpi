import pytest

from level_strategies import MedianThenRollingAverageStrategy, RawStrategy
from strategy_config import load_strategies


def write_yaml(tmp_path, content):
    path = tmp_path / "strategies.yaml"
    path.write_text(content)
    return path


def test_loads_valid_config(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
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

    strategies, primary_name = load_strategies(path)

    assert primary_name == "rolling_avg"
    assert set(strategies) == {"rolling_avg", "instantaneous_raw"}
    assert isinstance(strategies["rolling_avg"], MedianThenRollingAverageStrategy)
    assert isinstance(strategies["instantaneous_raw"], RawStrategy)


def test_missing_primary_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
          - name: raw
            type: raw
        """,
    )

    with pytest.raises(ValueError, match="primary"):
        load_strategies(path)


def test_multiple_primaries_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
          - name: a
            type: raw
            primary: true
          - name: b
            type: raw
            primary: true
        """,
    )

    with pytest.raises(ValueError, match="multiple strategies marked primary"):
        load_strategies(path)


def test_unknown_type_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
          - name: a
            type: exponential_moving_average
            primary: true
        """,
    )

    with pytest.raises(ValueError, match="unknown type"):
        load_strategies(path)


def test_duplicate_name_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
          - name: a
            type: raw
            primary: true
          - name: a
            type: raw
        """,
    )

    with pytest.raises(ValueError, match="duplicate strategy name"):
        load_strategies(path)


def test_invalid_params_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        strategies:
          - name: a
            type: median
            primary: true
            params:
              not_a_real_param: 5
        """,
    )

    with pytest.raises(ValueError, match="invalid params"):
        load_strategies(path)


def test_empty_strategies_list_raises(tmp_path):
    path = write_yaml(tmp_path, "strategies: []\n")

    with pytest.raises(ValueError, match="non-empty list"):
        load_strategies(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_strategies(tmp_path / "does_not_exist.yaml")
