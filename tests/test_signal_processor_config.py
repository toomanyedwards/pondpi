import pytest

from pondpi.signal_processor_config import load_signal_processors
from pondpi.signal_processors.chain_processor import ChainSignalProcessor
from pondpi.signal_processors.raw_processor import RawSignalProcessor


def write_yaml(tmp_path, content):
    path = tmp_path / "processors.yaml"
    path.write_text(content)
    return path


def test_loads_valid_config(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_median5
            type: rolling_median
            params:
              window_size: 5
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: rolling_median5
                - type: rolling_average
                  params:
                    window_size: 40
          - name: instantaneous_raw
            type: raw
        """,
    )

    processors, primary_name, emit_flags, configs = load_signal_processors(path)

    assert primary_name == "rolling_avg"
    assert set(processors) == {"rolling_median5", "rolling_avg", "instantaneous_raw"}
    assert isinstance(processors["rolling_avg"], ChainSignalProcessor)
    assert isinstance(processors["instantaneous_raw"], RawSignalProcessor)
    # emit defaults to True when not specified
    assert emit_flags == {"rolling_median5": True, "rolling_avg": True, "instantaneous_raw": True}
    assert configs["rolling_median5"] == {
        "type": "rolling_median",
        "params": {"window_size": 5},
        "primary": False,
        "emit": True,
    }
    assert configs["instantaneous_raw"] == {
        "type": "raw",
        "params": {},
        "primary": False,
        "emit": True,
    }


def test_emit_false_is_respected(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_median5
            type: rolling_median
            emit: false
            params:
              window_size: 5
          - name: instantaneous_raw
            type: raw
            primary: true
        """,
    )

    _, _, emit_flags, configs = load_signal_processors(path)

    assert emit_flags == {"rolling_median5": False, "instantaneous_raw": True}
    assert configs["rolling_median5"]["emit"] is False


def test_config_summary_reflects_effective_primary_and_chain_params(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_median5
            type: rolling_median
            params:
              window_size: 5
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: rolling_median5
                - type: rolling_average
                  params:
                    window_size: 40
        """,
    )

    _, _, _, configs = load_signal_processors(path)

    assert configs["rolling_avg"] == {
        "type": "chain",
        "primary": True,
        "emit": True,
        "params": {
            "steps": [
                {"ref": "rolling_median5"},
                {"type": "rolling_average", "params": {"window_size": 40}},
            ]
        },
    }


def test_chain_ref_step_builds_an_independent_instance(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_median5
            type: rolling_median
            params:
              window_size: 5
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: rolling_median5
                - type: rolling_average
                  params:
                    window_size: 2
        """,
    )

    processors, _, _, _ = load_signal_processors(path)
    rolling_median5 = processors["rolling_median5"]
    rolling_avg = processors["rolling_avg"]

    # Feed distinct values into the standalone rolling_median5 vs. the chain (which
    # also starts with a median-5 step). If the chain's ref step shared
    # rolling_median5's actual instance, these calls would corrupt each other's
    # window -- assert they stay fully independent.
    rolling_median5.add(100)
    rolling_avg.add(999)

    assert rolling_median5.extra_state()["samples_in_window"] == 1
    chain_median_state = rolling_avg.extra_state()["steps"][0]
    assert chain_median_state["samples_in_window"] == 1
    assert chain_median_state["processor"] == "rolling_median5"


def test_nested_chain_of_chains(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: outer
            type: chain
            primary: true
            params:
              steps:
                - type: chain
                  params:
                    steps:
                      - type: rolling_median
                        params:
                          window_size: 3
                - type: rolling_average
                  params:
                    window_size: 2
        """,
    )

    processors, _, _, _ = load_signal_processors(path)
    outer = processors["outer"]

    assert outer.add(10) == 10  # median([10]) = 10 -> rolling([10]) = 10

    # median([10, 30]) = 20 (2-element window, average of the two) -> rolling average of [10, 20] = 15
    assert outer.add(30) == 15


def test_ref_to_undefined_processor_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: does_not_exist
        """,
    )

    with pytest.raises(ValueError, match="references undefined processor 'does_not_exist'"):
        load_signal_processors(path)


def test_ref_to_processor_defined_later_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: rolling_median5
          - name: rolling_median5
            type: rolling_median
            params:
              window_size: 5
        """,
    )

    with pytest.raises(ValueError, match="references undefined processor 'rolling_median5'"):
        load_signal_processors(path)


def test_chain_step_self_reference_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps:
                - ref: rolling_avg
        """,
    )

    with pytest.raises(ValueError, match="references undefined processor 'rolling_avg'"):
        load_signal_processors(path)


def test_chain_with_empty_steps_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: rolling_avg
            type: chain
            primary: true
            params:
              steps: []
        """,
    )

    with pytest.raises(ValueError, match="must have at least one step"):
        load_signal_processors(path)


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
        load_signal_processors(path)


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
        load_signal_processors(path)


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
        load_signal_processors(path)


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
        load_signal_processors(path)


def test_invalid_params_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        processors:
          - name: a
            type: rolling_median
            primary: true
            params:
              not_a_real_param: 5
        """,
    )

    with pytest.raises(ValueError, match="invalid params"):
        load_signal_processors(path)


def test_empty_processors_list_raises(tmp_path):
    path = write_yaml(tmp_path, "processors: []\n")

    with pytest.raises(ValueError, match="non-empty list"):
        load_signal_processors(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_signal_processors(tmp_path / "does_not_exist.yaml")
