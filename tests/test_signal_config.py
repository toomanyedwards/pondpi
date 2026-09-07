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
              unit: cm
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            params:
              window_size: 5
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            params:
              window_size: 40
              poll_interval_ms: 1000
        """,
    )

    grouped = load_signals(path, {"pond_main"})
    group = grouped["pond_main"]

    assert set(group["signals"]) == {"instantaneous_raw", "rolling_median5", "rolling_avg"}
    assert isinstance(group["signals"]["instantaneous_raw"], SensorSignal)
    # emit defaults to True when not specified
    assert group["emit_flags"] == {"instantaneous_raw": True, "rolling_median5": True, "rolling_avg": True}
    assert group["configs"]["rolling_median5"] == {
        "type": "rolling_median",
        "params": {"window_size": 5},
        "emit": True,
        "input": "instantaneous_raw",
        "unit": "cm",
        "mode": "raw",
    }
    assert group["configs"]["instantaneous_raw"] == {
        "type": "sensor",
        "params": {"sensor": "pond_main", "unit": "cm"},
        "emit": True,
        "unit": "cm",
        "mode": "raw",
    }


def test_emit_false_is_respected(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            emit: false
            params:
              window_size: 5
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            params:
              window_size: 40
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]

    assert group["emit_flags"] == {"instantaneous_raw": True, "rolling_median5": False, "rolling_avg": True}
    assert group["configs"]["rolling_median5"]["emit"] is False


def test_signals_grouped_independently_per_sensor(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: pond_raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: pond_avg
            type: rolling_average
            input: pond_raw
            params:
              window_size: 2
              poll_interval_ms: 1000
          - name: barrel_raw
            type: sensor
            params:
              sensor: rain_barrel
              unit: cm
          - name: barrel_avg
            type: rolling_average
            input: barrel_raw
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    grouped = load_signals(path, {"pond_main", "rain_barrel"})

    assert set(grouped["pond_main"]["signals"]) == {"pond_raw", "pond_avg"}
    assert set(grouped["rain_barrel"]["signals"]) == {"barrel_raw", "barrel_avg"}


def test_downstream_signal_input_can_be_multiple_hops_away(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            params:
              window_size: 3
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]
    assert set(group["signals"]) == {"instantaneous_raw", "rolling_median5", "rolling_avg"}


def test_sensor_with_input_set_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            input: b
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
            params:
              sensor: not_a_real_sensor
        """,
    )

    with pytest.raises(ValueError, match="invalid or missing params.sensor 'not_a_real_sensor'"):
        load_signals(path, {"pond_main"})


def test_missing_unit_param_raises(tmp_path):
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

    with pytest.raises(ValueError, match="is missing required params.unit"):
        load_signals(path, {"pond_main"})


def test_non_sensor_setting_unit_directly_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: rolling_median
            input: a
            params:
              window_size: 5
              unit: cm
        """,
    )

    with pytest.raises(ValueError, match="must not set params.unit directly"):
        load_signals(path, {"pond_main"})


def test_downstream_signal_derives_unit_from_input(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: instantaneous_raw
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: rolling_median5
            type: rolling_median
            input: instantaneous_raw
            params:
              window_size: 3
          - name: rolling_avg
            type: rolling_average
            input: rolling_median5
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]

    # Neither downstream signal declares its own unit -- both inherit
    # "cm" transitively from instantaneous_raw, several hops away for
    # rolling_avg.
    assert group["configs"]["rolling_median5"]["unit"] == "cm"
    assert group["configs"]["rolling_avg"]["unit"] == "cm"


def test_sensor_mode_defaults_to_raw(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: rolling_average
            input: a
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]
    assert group["configs"]["a"]["mode"] == "raw"


def test_sensor_mode_processed_is_respected(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: sensor
            params:
              sensor: pond_main
              unit: cm
              mode: processed
          - name: c
            type: rolling_average
            input: a
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]
    assert group["configs"]["b"]["mode"] == "processed"


def test_unsupported_sensor_unit_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: mm
        """,
    )

    with pytest.raises(ValueError, match="invalid params.unit 'mm'"):
        load_signals(path, {"pond_main"})


def test_invalid_mode_param_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
              mode: smoothed
        """,
    )

    with pytest.raises(ValueError, match="invalid params.mode 'smoothed'"):
        load_signals(path, {"pond_main"})


def test_non_sensor_setting_mode_directly_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: rolling_median
            input: a
            params:
              window_size: 5
              mode: processed
        """,
    )

    with pytest.raises(ValueError, match="must not set params.mode directly"):
        load_signals(path, {"pond_main"})


def test_downstream_signal_derives_mode_from_input(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: sensor
            params:
              sensor: pond_main
              unit: cm
              mode: processed
          - name: c
            type: rolling_median
            input: b
            params:
              window_size: 2
          - name: d
            type: rolling_average
            input: a
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    group = load_signals(path, {"pond_main"})["pond_main"]
    assert group["configs"]["c"]["mode"] == "processed"


def test_sensor_with_no_signals_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: rolling_average
            input: a
            params:
              window_size: 2
              poll_interval_ms: 1000
        """,
    )

    with pytest.raises(ValueError, match="sensor 'rain_barrel' has no signals rooted at it"):
        load_signals(path, {"pond_main", "rain_barrel"})


def test_unknown_type_raises(tmp_path):
    path = write_yaml(
        tmp_path,
        """
        signals:
          - name: a
            type: not_a_real_type
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
            params:
              sensor: pond_main
              unit: cm
          - name: a
            type: sensor
            params:
              sensor: pond_main
              unit: cm
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
            params:
              sensor: pond_main
              unit: cm
          - name: b
            type: rolling_median
            input: a
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
