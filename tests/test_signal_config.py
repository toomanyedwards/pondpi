import pytest

from pondpi.signal_config import load_signals
from pondpi.signals.sensor_signal import SensorSignal


def write_yaml(tmp_path, content):
    path = tmp_path / "signals.yaml"
    path.write_text(content)
    return path


def test_loads_valid_config(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            params:
              sensor: pond_main
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            params:
              window_size: 5
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            primary: true
            params:
              window_size: 40
        """,
    )

    grouped = load_signals(path, {"pond_main"})
    group = grouped["pond_main"]

    assert group["primary_name"] == "rolling_avg"
    assert set(group["signals"]) == {"instantaneous_raw", "rolling_median5", "rolling_avg"}
    assert isinstance(group["signals"]["instantaneous_raw"], SensorSignal)
    # emit defaults to True when not specified
    assert group["emit_flags"] == {"instantaneous_raw": True, "rolling_median5": True, "rolling_avg": True}
    assert group["configs"]["rolling_median5"] == {
        "type": "rolling_median",
        "params": {"window_size": 5},
        "primary": False,
        "emit": True,
        "input": "instantaneous_raw",
    }
    assert group["configs"]["instantaneous_raw"] == {
        "type": "sensor",
        "params": {"sensor": "pond_main"},
        "primary": False,
        "emit": True,
    }


def test_emit_false_is_respected(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            primary: true
            params:
              sensor: pond_main
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            emit: false
            params:
              window_size: 5
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]

    assert group["emit_flags"] == {"instantaneous_raw": True, "rolling_median5": False}
    assert group["configs"]["rolling_median5"]["emit"] is False


def test_signals_grouped_independently_per_sensor(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: pond_raw
            type: sensor
            primary: true
            params:
              sensor: pond_main
          - name: barrel_raw
            type: sensor
            primary: true
            params:
              sensor: rain_barrel
          - name: pond_smoothed
            type: rolling_average
            input: pond_raw
            params:
              window_size: 2
        """,
    )

    grouped = load_signals(path, {"pond_main", "rain_barrel"})

    assert set(grouped["pond_main"]["signals"]) == {"pond_raw", "pond_smoothed"}
    assert set(grouped["rain_barrel"]["signals"]) == {"barrel_raw"}
    assert grouped["pond_main"]["primary_name"] == "pond_raw"
    assert grouped["rain_barrel"]["primary_name"] == "barrel_raw"


def test_downstream_signal_input_can_be_multiple_hops_away(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            params:
              sensor: pond_main
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            params:
              window_size: 3
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            primary: true
            params:
              window_size: 2
        """,
    )

    group = load_signals(path, {"pond_main"})
    assert group["pond_main"]["primary_name"] == "rolling_avg"


def test_sensor_with_input_set_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            input: b
            primary: true
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="is type 'sensor' and must not set 'input'"):
        load_signals(path, {"pond_main"})


def test_non_sensor_without_input_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: rolling_median
            primary: true
            params:
              window_size: 5
        """,
    )

    with pytest.raises(ValueError, match="must set 'input'"):
        load_signals(path, {"pond_main"})


def test_input_referencing_undefined_signal_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: rolling_median
            input: does_not_exist
            primary: true
            params:
              window_size: 5
        """,
    )

    with pytest.raises(ValueError, match="references undefined input 'does_not_exist'"):
        load_signals(path, {"pond_main"})


def test_input_referencing_signal_defined_later_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: rolling_median
            input: b
            primary: true
            params:
              window_size: 5
          - name: b
            type: sensor
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="references undefined input 'b'"):
        load_signals(path, {"pond_main"})


def test_input_self_reference_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: rolling_median
            input: a
            primary: true
            params:
              window_size: 5
        """,
    )

    with pytest.raises(ValueError, match="references undefined input 'a'"):
        load_signals(path, {"pond_main"})


def test_missing_sensor_param_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params: {}
        """,
    )

    with pytest.raises(ValueError, match="invalid or missing params.sensor"):
        load_signals(path, {"pond_main"})


def test_unknown_sensor_param_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params:
              sensor: not_a_real_sensor
        """,
    )

    with pytest.raises(ValueError, match="invalid or missing params.sensor 'not_a_real_sensor'"):
        load_signals(path, {"pond_main"})


def test_sensor_with_no_signals_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="sensor 'rain_barrel' has no signals rooted at it"):
        load_signals(path, {"pond_main", "rain_barrel"})


def test_missing_primary_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="exactly one signal must be marked 'primary: true'"):
        load_signals(path, {"pond_main"})


def test_multiple_primaries_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params:
              sensor: pond_main
          - name: b
            type: rolling_median
            input: a
            primary: true
            params:
              window_size: 5
        """,
    )

    with pytest.raises(ValueError, match="multiple signals marked primary"):
        load_signals(path, {"pond_main"})


def test_unknown_type_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: not_a_real_type
            primary: true
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="unknown type"):
        load_signals(path, {"pond_main"})


def test_duplicate_name_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params:
              sensor: pond_main
          - name: a
            type: sensor
            params:
              sensor: pond_main
        """,
    )

    with pytest.raises(ValueError, match="duplicate signal name"):
        load_signals(path, {"pond_main"})


def test_invalid_params_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            primary: true
            params:
              sensor: pond_main
          - name: b
            type: rolling_median
            input: a
            primary: true
            params:
              not_a_real_param: 5
        """,
    )

    with pytest.raises(ValueError, match="invalid params"):
        load_signals(path, {"pond_main"})


def test_empty_signals_list_raises(tmp_path):
    path = write_yaml(tmp_path, "signals: []\n")

    with pytest.raises(ValueError, match="non-empty list"):
        load_signals(path, {"pond_main"})


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_signals(tmp_path / "does_not_exist.yaml", {"pond_main"})
